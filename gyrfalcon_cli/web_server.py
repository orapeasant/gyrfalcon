"""Web Dashboard — FastAPI backend serving REST API, WebSocket PTY bridge, and React SPA."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.config import (
    load_config, save_config, cfg_get, get_all_env_vars,
    save_env_value, OPTIONAL_ENV_VARS, DEFAULT_CONFIG,
)
from gyrfalcon.gyrfalcon_constants import (
    get_gyrfalcon_home, get_config_path, get_env_path, get_app_name, get_org_name,
)
from gyrfalcon.scheduler import job_store
from gyrfalcon.tools.mcp_tool import mcp_store
from gyrfalcon.tools.skills_tool import discover_skills
from gyrfalcon.toolsets import TOOLSETS, list_toolsets
from gyrfalcon.telemetry import (
    init_telemetry, instrument_fastapi, instrument_httpx,
    WebSocketTracer, trace_span,
)

logger = get_logger("web_server")
mcp_logger = get_logger("mcp")   # routes to mcp.log via setup_logging

# Initialize OpenTelemetry. instrument_httpx() wraps the transport the LLM SDK
# uses, so leaving it on unconditionally taxes every model call.
_telemetry_enabled = cfg_get("performance.telemetry_enabled", False)
if _telemetry_enabled:
    init_telemetry(service_name="gyrfalcon-dashboard")
    instrument_httpx()

# Session token for authentication
_session_token = secrets.token_urlsafe(32)
_local_password_auth_ready = False
_local_password_auth_lock = threading.Lock()
_local_login_failures: dict[str, tuple[int, float]] = {}
_local_login_failures_lock = threading.Lock()

app = FastAPI(title=f"{get_app_name()} Dashboard API")

# Instrument FastAPI with OpenTelemetry
if _telemetry_enabled:
    instrument_fastapi(app)


def _verify_token(request: Request):
    """Verify the session token and bind the caller's identity.

    Returns the `Principal`, and binds it for the rest of this request — which
    is what makes every store call below tenant-scoped without each endpoint
    having to remember (§17.5).

    Binding without a matching reset is safe here specifically: ASGI runs each
    request in its own task, and a task gets its own copy of the context, so
    the set cannot leak into another request. It is done here rather than in
    middleware because Starlette's BaseHTTPMiddleware runs the endpoint in a
    *separate* task, and a contextvar set in middleware would not reach it.

    The shared token is still one credential for the whole server: it proves
    "an operator of this install", not "which person". Real per-user
    authentication is §17.11 step 8; until then this resolves to the LOCAL
    principal, which is a real filtered identity rather than a bypass.
    """
    logger.debug("Beginning of _verify_token")
    from gyrfalcon import identity

    local_password_auth = _ensure_local_password_auth()
    if identity.identity_enabled() or local_password_auth:
        # Per-user credentials only: a shared secret cannot say who is calling.
        return _bind_request_principal(request)

    token = request.headers.get("X-Gyrfalcon-Session-Token")
    if token == _session_token:
        return _bind_request_principal(request)

    # A Service Account's OAuth2 access token (Administration > Security) is
    # a second valid credential here even with per-user identity off — this
    # is what actually lets an external application call the API without
    # ever holding the shared dashboard token. Checked before rejecting, not
    # inside `_bind_request_principal`, because that function runs only
    # *after* this gate already passed for the X-Gyrfalcon-Session-Token
    # case above — an OAuth caller has no session token to pass that gate
    # with.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        from gyrfalcon.security import validate_access_token

        if validate_access_token(auth_header[7:].strip()) is not None:
            return _bind_request_principal(request)

    raise HTTPException(status_code=401, detail="Invalid session token")


def _bind_request_principal(request: Request):
    """Resolve the caller to a `Principal` and bind it for this request.

    Precedence, most specific first:

    1. `Authorization: Bearer gyr_live_…` — a per-user API key, for
       non-interactive callers.
    2. The session cookie from an OIDC login.
    3. The shared dashboard token — **only while identity is disabled**.

    Rule 3 is the one that matters. The shared token proves "an operator of
    this install", not *which person*, so once identity is enabled it must not
    grant access: otherwise it is a single credential that bypasses every
    tenant boundary built in §17. With identity off it resolves to the LOCAL
    principal, which is a real filtered identity rather than a bypass.
    """
    from gyrfalcon import identity

    principal = _resolve_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    identity._PRINCIPAL.set(principal)
    return principal


def _resolve_principal(request: Request):
    from gyrfalcon import identity

    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        token = header[7:].strip()
        from gyrfalcon.auth.store import get_auth_store

        principal = get_auth_store().authenticate_api_key(token)
        if principal is not None:
            return principal

        # Service accounts are distinct tenant-scoped callers. In particular,
        # flow invocations must retain the actual caller in their run record.
        from gyrfalcon.security import validate_access_token

        claims = validate_access_token(token)
        if claims is not None:
            return identity.Principal(
                user_id=f"service:{claims['service_account_id']}",
                tenant_id=claims["tenant_id"],
                display_name=claims["service_account_name"],
                roles=frozenset(claims["scopes"]),
                source="oauth",
            )

    from gyrfalcon.auth.session import COOKIE_NAME, get_session_store

    session = get_session_store().get(request.cookies.get(COOKIE_NAME))
    if session is not None:
        from gyrfalcon.auth.store import get_auth_store

        principal = get_auth_store().principal_for(session.user_id, session.org_id)
        if principal is not None:
            return principal

    # Once local password auth is active, the single-user LOCAL fallback must
    # not turn a missing or expired browser cookie into an authenticated user.
    if _ensure_local_password_auth():
        return None

    if not identity.identity_enabled():
        return identity.LOCAL
    return None


def _ensure_local_password_auth() -> bool:
    """Bootstrap the one-time local admin and cache whether password auth is active."""
    global _local_password_auth_ready
    if _local_password_auth_ready:
        return True
    with _local_password_auth_lock:
        if _local_password_auth_ready:
            return True
        try:
            from gyrfalcon.auth.store import get_auth_store
            store = get_auth_store()
            store.ensure_default_admin()
            _local_password_auth_ready = store.has_local_credentials()
        except Exception:
            logger.exception("Could not initialize local dashboard credentials")
            raise HTTPException(503, "Dashboard authentication is unavailable")
    return _local_password_auth_ready


# --- Status ---

# Track engine state - the dashboard server itself hosts the engine
_engine_initialized = False

# --- Agent event bus ---
# Maps session_id → list of WebSocket send coroutines subscribed to that session.
# Background agent threads push events here; the WS handler forwards them.
import asyncio as _asyncio
import threading as _ebus_lock_mod

_agent_event_subscribers: dict[str, list] = {}   # session_id → [(loop, asyncio.Queue)]
_agent_event_backlog: dict[str, list] = {}       # session_id → events of the in-flight run
_agent_event_lock = _ebus_lock_mod.Lock()

# A long run can emit thousands of stream deltas. Only the tail is worth
# replaying to a client that connects late — the terminal message.complete
# carries the full text regardless.
_AGENT_BACKLOG_MAX = 2000


def _agent_run_begin(session_id: str) -> None:
    """Mark a session as having an in-flight agent run.

    Called before the worker thread starts, so the backlog exists the moment
    the invoke response reaches the browser. `_maybe_subscribe_agent` uses the
    presence of this entry to decide whether an agent is running — subscribers
    cannot be the signal, because the first client to connect is by definition
    not yet subscribed.
    """
    with _agent_event_lock:
        _agent_event_backlog[session_id] = []


def _agent_run_end(session_id: str) -> None:
    """Drop the backlog once the run is over.

    Ordering matters: this runs strictly after the agent has persisted its
    final message, so a client that subscribes too late to replay is
    guaranteed to see the same content via session.resume instead.
    """
    with _agent_event_lock:
        _agent_event_backlog.pop(session_id, None)


def _agent_event_subscribe(session_id: str, queue: "_asyncio.Queue", loop) -> list[dict]:
    """Register a queue and return the events it missed, atomically."""
    with _agent_event_lock:
        _agent_event_subscribers.setdefault(session_id, []).append((loop, queue))
        return list(_agent_event_backlog.get(session_id, []))


def _agent_event_unsubscribe(session_id: str, queue: "_asyncio.Queue") -> None:
    with _agent_event_lock:
        subs = _agent_event_subscribers.get(session_id, [])
        for entry in list(subs):
            if entry[1] is queue:
                subs.remove(entry)
        if not subs:
            _agent_event_subscribers.pop(session_id, None)


def _agent_event_publish(session_id: str, event: dict) -> None:
    """Called from a background thread — hand the event to each subscriber's loop.

    asyncio.Queue is not thread-safe and put_nowait from a foreign thread does
    not reliably wake the loop, so the put is scheduled onto the owning loop.
    """
    with _agent_event_lock:
        backlog = _agent_event_backlog.get(session_id)
        if backlog is not None:
            backlog.append(event)
            if len(backlog) > _AGENT_BACKLOG_MAX:
                del backlog[: len(backlog) - _AGENT_BACKLOG_MAX]
        targets = list(_agent_event_subscribers.get(session_id, []))
    for loop, q in targets:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            pass  # loop already closed


@app.get("/api/status")
async def get_status(request: Request):
    _verify_token(request)
    from gyrfalcon import __version__
    return {
        "version": __version__,
        "gyrfalcon_home": str(get_gyrfalcon_home()),
        "config_path": str(get_config_path()),
        "env_path": str(get_env_path()),
        "config_version": cfg_get("_config_version", 1),
        "gateway_running": True,  # Dashboard server IS the engine
        "gateway_pid": os.getpid(),
        "active_sessions": 0,
        "engine_ready": True,
    }


@app.post("/api/engine/start")
async def start_engine(request: Request):
    """Start/ensure the gyrfalcon engine is ready for chat.
    
    Since the dashboard server hosts the engine, this just confirms readiness
    and initializes any lazy components.
    """
    _verify_token(request)
    global _engine_initialized
    
    # Initialize MCP servers if not already done
    if not _engine_initialized:
        try:
            from gyrfalcon.tools.mcp_tool import initialize_mcp_servers
            initialize_mcp_servers()
            _engine_initialized = True
            logger.info("Engine initialized via dashboard start request")
        except Exception as e:
            logger.warning(f"MCP initialization skipped: {e}")
            _engine_initialized = True
    
    return {"status": "running", "pid": os.getpid()}


@app.get("/api/telemetry/status")
async def get_telemetry_status(request: Request):
    """Get OpenTelemetry configuration status."""
    _verify_token(request)
    return {
        "enabled": True,
        "service_name": "gyrfalcon-dashboard",
        "otlp_endpoint": os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", None),
        "console_export": os.getenv("OTEL_CONSOLE_EXPORT", "false").lower() == "true",
        "instrumentation": {
            "fastapi": True,
            "httpx": True,
            "websocket": True,
            "agent": True,
        },
        "env_vars": {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "Set OTLP collector endpoint (e.g., http://localhost:4317)",
            "OTEL_CONSOLE_EXPORT": "Set to 'true' to enable console span export for debugging",
            "GYRFALCON_ENV": "Environment name for resource attributes (default: development)",
        }
    }


# --- Debug: Token check (no auth required) ---

@app.get("/api/debug/token")
async def debug_token(request: Request):
    """Debug endpoint to check token - compare with window.__GYRFALCON_SESSION_TOKEN__ in browser."""
    if _ensure_local_password_auth():
        _verify_token(request)
    client_token = request.query_params.get("token", "")
    expected_preview = f"{_session_token[:8]}...{_session_token[-4:]}"
    client_preview = f"{client_token[:8]}...{client_token[-4:]}" if client_token and len(client_token) > 12 else client_token or "(empty)"
    return {
        "expected_token_preview": expected_preview,
        "client_token_preview": client_preview,
        "match": client_token == _session_token,
        "expected_length": len(_session_token),
        "client_length": len(client_token) if client_token else 0,
    }


# --- Sessions ---

@app.get("/api/sessions")
async def list_sessions(request: Request, limit: int = 50, offset: int = 0):
    _verify_token(request)
    db = SessionDB()
    try:
        sessions = db.list_sessions(limit=limit, offset=offset)
        total = db.count_sessions()
        return {"sessions": sessions, "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


class BatchDeleteRequest(BaseModel):
    ids: list[str]


# Fixed-path sub-routes MUST come before /{session_id} to avoid being matched as a session_id
@app.delete("/api/sessions/batch")
async def delete_sessions_batch(request: Request, body: BatchDeleteRequest):
    _verify_token(request)
    db = SessionDB()
    try:
        for sid in body.ids:
            db.delete_session(sid)
        return {"deleted": len(body.ids)}
    finally:
        db.close()


@app.get("/api/sessions/search")
async def search_sessions(request: Request, q: str, limit: int = 10):
    _verify_token(request)
    db = SessionDB()
    try:
        results = db.search_messages(q, limit=limit)
        return {"results": results}
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(request: Request, session_id: str):
    _verify_token(request)
    db = SessionDB()
    try:
        messages = db.get_messages(session_id)
        return {"messages": messages}
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/trajectory")
async def get_session_trajectory(request: Request, session_id: str):
    """Return the session's agent steps: LLM turns with their tool calls and results."""
    _verify_token(request)
    from gyrfalcon.agent.trajectory import build_trajectory
    db = SessionDB()
    try:
        if not db.get_session(session_id):
            raise HTTPException(404, f"Session not found: {session_id}")
        trajectory = build_trajectory(db.get_messages(session_id))
        return {"session_id": session_id, **trajectory}
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/stats")
async def get_session_stats(request: Request, session_id: str):
    """Return token counts and cost for a session."""
    _verify_token(request)
    from gyrfalcon.pricing import usd_to_aic, format_cost
    db = SessionDB()
    try:
        session = db.get_session(session_id)
        if not session:
            raise HTTPException(404, f"Session not found: {session_id}")
        in_tok     = int(session.get("input_tokens", 0) or 0)
        out_tok    = int(session.get("output_tokens", 0) or 0)
        reason_tok = int(session.get("reasoning_tokens", 0) or 0)
        cost_usd   = float(session.get("cost", 0.0) or 0.0)
        provider   = cfg_get("provider.active", "copilot")
        msgs       = db.get_messages(session_id)
        return {
            "session_id":       session_id,
            "model":            session.get("model", ""),
            "provider":         provider,
            "input_tokens":     in_tok,
            "output_tokens":    out_tok,
            "reasoning_tokens": reason_tok,
            "total_tokens":     in_tok + out_tok + reason_tok,
            "cost_usd":         round(cost_usd, 6),
            "cost_aic":         usd_to_aic(cost_usd),
            "cost_display":     format_cost(cost_usd, provider),
            "message_count":    len(msgs),
            "started_at":       session.get("started_at"),
            "last_active":      session.get("last_active"),
        }
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session(request: Request, session_id: str):
    _verify_token(request)
    db = SessionDB()
    try:
        db.delete_session(session_id)
        return {"status": "deleted"}
    finally:
        db.close()


# --- Config ---

@app.get("/api/config")
async def get_config(request: Request):
    _verify_token(request)
    return load_config()


@app.put("/api/config")
async def update_config(request: Request):
    _verify_token(request)
    body = await request.json()
    config = load_config()
    # Deep merge
    def merge(base, update):
        logger.debug("Beginning of merge")
        for k, v in update.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                merge(base[k], v)
            else:
                base[k] = v
    merge(config, body)
    save_config(config)
    return {"status": "saved"}


@app.get("/api/config/defaults")
async def get_config_defaults(request: Request):
    _verify_token(request)
    return DEFAULT_CONFIG


@app.get("/api/config/raw")
async def get_config_raw(request: Request):
    _verify_token(request)
    config_path = get_config_path()
    if config_path.exists():
        return {"content": config_path.read_text()}
    return {"content": ""}


@app.put("/api/config/raw")
async def put_config_raw(request: Request):
    _verify_token(request)
    body = await request.json()
    content = body.get("content", "")
    config_path = get_config_path()
    config_path.write_text(content)
    return {"status": "saved"}


# --- Environment Variables ---

@app.get("/api/env")
async def get_env_vars(request: Request):
    _verify_token(request)
    env_vars = get_all_env_vars()
    # Mask passwords
    masked = {}
    for key, value in env_vars.items():
        info = OPTIONAL_ENV_VARS.get(key, {})
        if info.get("password"):
            masked[key] = {"value": "***", "set": True, "category": info.get("category", "")}
        else:
            masked[key] = {"value": value, "set": True, "category": info.get("category", "")}
    return {"variables": masked, "schema": OPTIONAL_ENV_VARS}


@app.put("/api/env")
async def set_env_var(request: Request):
    _verify_token(request)
    body = await request.json()
    key = body.get("key", "")
    value = body.get("value", "")
    if not key:
        raise HTTPException(400, "key is required")
    save_env_value(key, value)
    return {"status": "saved"}


@app.delete("/api/env/{key}")
async def delete_env_var(request: Request, key: str):
    _verify_token(request)
    # Remove from .env file
    env_path = get_env_path()
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        lines = [l for l in lines if not l.startswith(f"{key}=")]
        env_path.write_text("\n".join(lines) + "\n")
    return {"status": "deleted"}


@app.post("/api/env/reveal")
async def reveal_env_var(request: Request):
    _verify_token(request)
    body = await request.json()
    key = body.get("key", "")
    from gyrfalcon.config import get_env_value
    value = get_env_value(key)
    return {"key": key, "value": value or ""}


# --- Models ---

@app.get("/api/model/info")
async def get_model_info(request: Request):
    _verify_token(request)
    config = load_config()
    return {
        "model": config.get("model", {}),
        "current": cfg_get("model.name", ""),
    }


@app.get("/api/model/state")
async def get_model_state(request: Request):
    """Return current provider/model and all providers with authentication status."""
    _verify_token(request)
    import os
    from gyrfalcon.providers import list_providers, get_provider_profile
    from gyrfalcon.providers.copilot import is_authenticated as copilot_is_authenticated

    current_provider = cfg_get("provider.active", "copilot")
    current_model = cfg_get("model.name", "") or "gpt-4o"

    providers_out = []
    for pname in list_providers():
        profile = get_provider_profile(pname)
        if not profile:
            continue
        authenticated = False
        if profile.auth_type == "copilot":
            authenticated = copilot_is_authenticated()
        elif profile.auth_type == "api_key":
            authenticated = any(bool(os.environ.get(ev)) for ev in profile.env_vars)
        elif profile.auth_type == "aws_sdk":
            authenticated = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
        providers_out.append({
            "name": profile.name,
            "display_name": profile.display_name or profile.name.title(),
            "auth_type": profile.auth_type,
            "authenticated": authenticated,
            "default_model": profile.default_model or "",
        })

    return {
        "provider": current_provider,
        "model": current_model,
        "providers": providers_out,
    }


@app.get("/api/model/providers/{provider_name}/models")
async def get_provider_models(request: Request, provider_name: str):
    """Fetch available models for a given provider (live + fallback)."""
    _verify_token(request)
    from gyrfalcon.providers import get_provider_profile

    profile = get_provider_profile(provider_name)
    if not profile:
        raise HTTPException(404, f"Unknown provider: {provider_name}")
    try:
        models = profile.fetch_models()
    except Exception as e:
        logger.warning(f"fetch_models failed for {provider_name}: {e}")
        models = profile.fallback_models or []
    return {"models": models, "provider": provider_name}


@app.get("/api/model/options")
async def get_model_options(request: Request):
    _verify_token(request)
    from gyrfalcon.providers import list_providers, get_provider_profile
    providers = list_providers()
    options = {}
    for name in providers:
        profile = get_provider_profile(name)
        if profile:
            options[name] = {
                "display_name": profile.display_name,
                "models": profile.fallback_models,
                "auth_type": profile.auth_type,
            }
    return {"providers": options}


@app.post("/api/model/set")
async def set_model(request: Request):
    """Persist active provider and model selection."""
    _verify_token(request)
    body = await request.json()
    model = body.get("model", "")
    provider = body.get("provider", "")
    if not model:
        raise HTTPException(400, "model is required")
    config = load_config()
    config["model"]["name"] = model
    if provider:
        config.setdefault("provider", {})["active"] = provider
    save_config(config)
    return {"status": "saved", "model": model, "provider": provider}


# --- Tokenomics (spec 19-tokenomics.md) ---

@app.get("/api/tokenomics/models")
async def tokenomics_models(request: Request, q: str = "", limit: int = 200):
    """Models the estimator can price, with how each one would be counted.

    Deliberately cheap: `describe()` reports a model's counting method without
    counting anything, so opening the picker costs no `count_tokens` calls.
    """
    _verify_token(request)
    from gyrfalcon.pricing import catalog as price_catalog
    from gyrfalcon.tokenizers import describe

    needle = (q or "").lower().strip()
    rows = []
    for name, rates in price_catalog.load().items():
        if needle and needle not in name:
            continue
        info = describe(name)
        rows.append({
            "model": name,
            "provider": rates.provider,
            "billing": rates.billing,
            "input_per_1k": rates.input,
            "output_per_1k": rates.output,
            "cache_read_per_1k": rates.effective_cache_read(),
            "cache_write_per_1k": rates.effective_cache_write("5m"),
            "context_window": rates.context_window,
            "countable": info["available"],
            "method": info["method"],
            "tokenizer": info["tokenizer"],
            "detail": info["detail"],
        })
    # Countable models first, then cheapest — the useful default ordering.
    rows.sort(key=lambda r: (not r["countable"], r["input_per_1k"], r["model"]))
    return {
        "models": rows[:limit],
        "total": len(rows),
        "catalog": price_catalog.meta(),
    }


@app.post("/api/tokenomics/upload")
async def tokenomics_upload(request: Request, file: UploadFile = File(...)):
    """Extract text from an uploaded document for estimating.

    The extracted text is held in memory with a TTL and is never written to
    disk or into the session store (§19.5.3).
    """
    _verify_token(request)
    from gyrfalcon.documents import MAX_BYTES, extract, store_upload

    data = await file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(
            413, f"File exceeds the {MAX_BYTES // (1024 * 1024)} MB limit"
        )

    extracted = extract(file.filename or "upload", data)
    upload = store_upload(extracted)
    return {
        "id": upload.id,
        "filename": upload.filename,
        "chars": len(upload.text),
        "pages": upload.pages,
        "warnings": upload.warnings,
        "empty": not upload.text.strip(),
        "preview": upload.text[:500],
    }


@app.post("/api/tokenomics/estimate")
async def tokenomics_estimate(request: Request):
    """Estimate token cost for text across models. Makes no LLM call."""
    _verify_token(request)
    from gyrfalcon.tokenomics.estimate import DEFAULT_OUTPUT_TOKENS, estimate

    body = await request.json()
    models = body.get("models") or []
    if not models:
        raise HTTPException(400, "models is required")
    if len(models) > 12:
        raise HTTPException(400, "at most 12 models per estimate")

    text = body.get("text") or ""
    upload_id = body.get("upload_id")
    if upload_id:
        from gyrfalcon.documents import get_upload

        upload = get_upload(upload_id)
        if upload is None:
            raise HTTPException(404, "upload expired or not found")
        text = upload.text
    if not text:
        raise HTTPException(400, "text or upload_id is required")

    try:
        output_tokens = int(body.get("output_tokens", DEFAULT_OUTPUT_TOKENS))
    except (TypeError, ValueError):
        output_tokens = DEFAULT_OUTPUT_TOKENS

    result = estimate(
        text=text,
        models=models,
        output_tokens=max(0, output_tokens),
        include_agent_prompt=bool(body.get("include_agent_prompt", True)),
        enabled_toolsets=body.get("toolsets") or None,
        cache_ttl=body.get("cache_ttl") or "5m",
    )
    return result.as_dict()


@app.get("/api/tokenomics/report")
async def tokenomics_report(
    request: Request,
    start: str,
    end: str,
    grain: str = "day",
    dimension: str = "model",
    pivot: str = "none",
):
    """Scoped usage report, grouped into UTC calendar periods and dimensions."""
    principal = _verify_token(request)
    from datetime import date, datetime, time as datetime_time, timedelta, timezone

    if grain not in {"day", "week", "month", "quarter"}:
        raise HTTPException(400, "grain must be day, week, month, or quarter")
    dimensions = {
        "none", "user", "group", "department", "business_unit", "model", "provider",
    }
    if dimension not in dimensions or pivot not in dimensions:
        raise HTTPException(400, "unsupported report dimension")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(400, "start and end must be YYYY-MM-DD dates") from exc
    if end_date < start_date:
        raise HTTPException(400, "end must be on or after start")
    if (end_date - start_date).days > 3660:
        raise HTTPException(400, "report range cannot exceed 10 years")

    start_at = datetime.combine(start_date, datetime_time.min, timezone.utc).timestamp()
    end_at = datetime.combine(
        end_date + timedelta(days=1), datetime_time.min, timezone.utc,
    ).timestamp()
    from gyrfalcon.db.scope import Scope
    from gyrfalcon.sessions.store import get_session_store

    rows = get_session_store().tokenomics_report(
        start_at, end_at, grain=grain, dimension=dimension, pivot=pivot,
        scope=Scope.of(principal),
    )
    totals = {
        "calls": sum(int(row["calls"] or 0) for row in rows),
        "input_tokens": sum(int(row["input_tokens"] or 0) for row in rows),
        "output_tokens": sum(int(row["output_tokens"] or 0) for row in rows),
        "reasoning_tokens": sum(int(row["reasoning_tokens"] or 0) for row in rows),
        "cost_usd": sum(float(row["cost_usd"] or 0) for row in rows),
    }
    return {"rows": rows, "totals": totals, "timezone": "UTC"}


@app.post("/api/pricing/refresh")
async def tokenomics_pricing_refresh(request: Request):
    """Refresh the price catalog from the public feed."""
    _verify_token(request)
    from gyrfalcon.pricing import catalog as price_catalog

    result = price_catalog.refresh()
    result["catalog"] = price_catalog.meta()
    return result


# --- Analytics ---

@app.get("/api/analytics/usage")
async def get_analytics(request: Request, days: int = 30):
    _verify_token(request)
    db = SessionDB()
    try:
        data = db.get_analytics(days=days)
        return {"daily": data}
    finally:
        db.close()


# --- Scheduler ---

@app.get("/api/scheduler/jobs")
async def list_scheduler_jobs(request: Request):
    _verify_token(request)
    return {"jobs": job_store.list_all()}


@app.post("/api/scheduler/jobs")
async def create_scheduler_job(request: Request):
    _verify_token(request)
    body = await request.json()
    job_id = job_store.add(body)
    return {"status": "created", "job_id": job_id}


@app.put("/api/scheduler/jobs/{job_id}")
async def update_scheduler_job(request: Request, job_id: str):
    """Update an existing scheduled job (re-parses schedule, recalculates next_run_at)."""
    _verify_token(request)
    body = await request.json()
    from gyrfalcon.scheduler import _build_job, next_run_iso
    existing = job_store.get(job_id)
    if not existing:
        raise HTTPException(404, "Job not found")
    # Merge updates; re-parse schedule if it changed
    updated = {**existing, **body, "id": job_id}
    schedule_raw = body.get("schedule_raw") or body.get("schedule", "")
    if schedule_raw and schedule_raw != existing.get("schedule_raw", ""):
        from gyrfalcon.scheduler import parse_schedule
        sched = parse_schedule(schedule_raw, use_llm=False)
        updated["schedule"] = sched
        updated["schedule_display"] = sched.get("display", "")
        updated["schedule_raw"] = sched.get("natural", schedule_raw)
        updated["next_run_at"] = next_run_iso(sched)
    job_store.update(job_id, updated)
    return {"status": "updated", "job_id": job_id, "job": job_store.get(job_id)}


@app.delete("/api/scheduler/jobs/{job_id}")
async def delete_scheduler_job(request: Request, job_id: str):
    _verify_token(request)
    if job_store.remove(job_id):
        return {"status": "deleted"}
    raise HTTPException(404, "Job not found")


@app.post("/api/scheduler/jobs/{job_id}/run")
async def run_scheduler_job_now(request: Request, job_id: str):
    """Manually trigger a scheduled job immediately as a chat session.

    Runs the job's prompt via AIAgent (platform='chat') in a background thread
    so the call returns instantly with a session_id the UI can navigate to.
    """
    _verify_token(request)
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("prompt") and not job.get("skill"):
        raise HTTPException(400, "Job has no prompt or skill configured")

    import uuid as _uuid
    session_id = str(_uuid.uuid4())

    def _run() -> None:
        try:
            from gyrfalcon.run_agent import AIAgent
            from gyrfalcon.config import cfg_get
            from gyrfalcon.scheduler import scheduler as _sched

            agent = AIAgent(
                model=job.get("model") or cfg_get("model.name", ""),
                provider=job.get("provider") or cfg_get("provider.active", "copilot"),
                base_url=job.get("base_url") or None,
                max_iterations=30,
                quiet_mode=False,
                skip_memory=False,
                platform="chat",
                session_id=session_id,
                enabled_toolsets=job.get("enabled_toolsets"),
            )
            # Use same runtime prompt enrichment as the scheduler
            enriched = _sched._build_runtime_prompt(job)
            agent.chat(enriched)
            logger.info(f"Manual scheduler run {job_id} -> session {session_id} completed")
        except Exception as e:
            logger.error(f"Manual scheduler run {job_id} failed: {e}")

    import threading as _threading
    t = _threading.Thread(target=_run, daemon=True,
                          name=f"scheduler-manual-{job_id[:6]}")
    t.start()

    return {"status": "started", "session_id": session_id, "job_id": job_id}


@app.post("/api/scheduler/jobs/{job_id}/pause")
async def pause_scheduler_job(request: Request, job_id: str):
    _verify_token(request)
    job_store.update(job_id, {"status": "paused"})
    return {"status": "paused"}


@app.post("/api/scheduler/jobs/{job_id}/resume")
async def resume_scheduler_job(request: Request, job_id: str):
    _verify_token(request)
    job_store.update(job_id, {"status": "active"})
    return {"status": "resumed"}


@app.get("/api/scheduler/jobs/{job_id}/history")
async def get_scheduler_history(request: Request, job_id: str):
    """List all run output files for a job, newest first."""
    _verify_token(request)
    from gyrfalcon.scheduler import _get_output_dir
    out_dir = _get_output_dir() / job_id
    if not out_dir.exists():
        return {"runs": []}
    runs = []
    for f in sorted(out_dir.glob("*.md"), reverse=True):
        stat = f.stat()
        runs.append({
            "run_id": f.stem,
            "filename": f.name,
            "timestamp": f.stem,     # "20260701_112957"
            "size_bytes": stat.st_size,
        })
    return {"runs": runs[:100]}     # latest 100


@app.get("/api/scheduler/jobs/{job_id}/history/{run_id}")
async def get_scheduler_run_detail(request: Request, job_id: str, run_id: str):
    """Read the output markdown of a specific scheduled run."""
    _verify_token(request)
    from gyrfalcon.scheduler import _get_output_dir
    out_dir = _get_output_dir() / job_id
    # Accept stem (without .md) or full filename
    candidates = [out_dir / f"{run_id}.md", out_dir / run_id]
    content = None
    for p in candidates:
        if p.exists():
            content = p.read_text(errors="replace")
            break
    if content is None:
        raise HTTPException(404, f"Run not found: {run_id}")
    return {"run_id": run_id, "content": content}


# --- Skills ---

@app.get("/api/skills")
async def list_skills_api(request: Request):
    _verify_token(request)
    skills = discover_skills()
    return {"skills": skills}


@app.get("/api/skills/{name:path}")
async def get_skill_api(request: Request, name: str):
    _verify_token(request)
    from gyrfalcon.tools.skills_tool import skill_view
    result = skill_view({"name": name})
    import json as _json
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(404, data["error"])
    return data


class SkillSaveRequest(BaseModel):
    content: str


@app.put("/api/skills/{name:path}")
async def save_skill_api(request: Request, name: str, body: SkillSaveRequest):
    _verify_token(request)
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    result = skill_manage({"action": "edit", "name": name, "content": body.content})
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(400, data["error"])
    return data


class SkillCreateRequest(BaseModel):
    name: str
    content: str
    description: str = ""


@app.post("/api/skills")
async def create_skill_api(request: Request, body: SkillCreateRequest):
    _verify_token(request)
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    result = skill_manage({
        "action": "create",
        "name": body.name,
        "content": body.content,
        "description": body.description,
    })
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(400, data["error"])
    return data


@app.delete("/api/skills/{name:path}")
async def delete_skill_api(request: Request, name: str):
    _verify_token(request)
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    result = skill_manage({"action": "delete", "name": name})
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(400, data["error"])
    return data


# --- Skill sub-files (references / scripts) ---

class SkillFileRequest(BaseModel):
    content: str


@app.get("/api/skills/{name}/files/{subdir}")
async def list_skill_files(request: Request, name: str, subdir: str):
    """List reference or script files for a skill."""
    _verify_token(request)
    if subdir not in ("references", "scripts"):
        raise HTTPException(400, "subdir must be 'references' or 'scripts'")
    from gyrfalcon.tools.skills_tool import skill_name_to_folder, _list_dir_files
    from gyrfalcon.gyrfalcon_constants import get_skills_dir
    folder = skill_name_to_folder(name)
    d = get_skills_dir() / folder / subdir
    return {"files": _list_dir_files(d)}


@app.get("/api/skills/{name}/files/{subdir}/{filename}")
async def get_skill_file(request: Request, name: str, subdir: str, filename: str):
    """Read a single reference or script file."""
    _verify_token(request)
    if subdir not in ("references", "scripts"):
        raise HTTPException(400, "subdir must be 'references' or 'scripts'")
    from gyrfalcon.tools.skills_tool import skill_name_to_folder
    from gyrfalcon.gyrfalcon_constants import get_skills_dir
    folder    = skill_name_to_folder(name)
    file_path = get_skills_dir() / folder / subdir / filename
    if not file_path.exists():
        raise HTTPException(404, f"File not found: {filename}")
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return {"name": filename, "content": content}


@app.put("/api/skills/{name}/files/{subdir}/{filename}")
async def save_skill_file(request: Request, name: str, subdir: str, filename: str, body: SkillFileRequest):
    """Create or overwrite a reference or script file."""
    _verify_token(request)
    if subdir not in ("references", "scripts"):
        raise HTTPException(400, "subdir must be 'references' or 'scripts'")
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    action = "add_reference" if subdir == "references" else "add_script"
    result = skill_manage({"action": action, "name": name, "filename": filename, "content": body.content})
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(400, data["error"])
    return data


@app.delete("/api/skills/{name}/files/{subdir}/{filename}")
async def delete_skill_file(request: Request, name: str, subdir: str, filename: str):
    """Delete a reference or script file."""
    _verify_token(request)
    if subdir not in ("references", "scripts"):
        raise HTTPException(400, "subdir must be 'references' or 'scripts'")
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    action = "delete_reference" if subdir == "references" else "delete_script"
    result = skill_manage({"action": action, "name": name, "filename": filename})
    data = _json.loads(result)
    if "error" in data:
        raise HTTPException(400, data["error"])
    return data


@app.post("/api/skills/migrate")
async def migrate_skills_api(request: Request):
    """Migrate all legacy flat .md skill files to folder-based structure."""
    _verify_token(request)
    from gyrfalcon.tools.skills_tool import skill_manage
    import json as _json
    result = skill_manage({"action": "migrate", "name": "_all"})
    return _json.loads(result)


# --- MCP Servers ---

def _mcp_server_entry(name: str, cfg: dict) -> dict:
    """Normalise a raw MCP config dict for the API response."""
    from gyrfalcon.tools.mcp_tool import get_mcp_connection
    conn = get_mcp_connection(name)
    tools = list(conn._tools.keys()) if conn else []
    return {
        "name": name,
        "type": cfg.get("type", "stdio"),
        "command": cfg.get("command", []),
        "args": cfg.get("args", []),
        "env": cfg.get("env", {}),
        "enabled": cfg.get("enabled", True),
        "connected": conn is not None,
        "tools": tools,
        "url": cfg.get("url", ""),
        "service": cfg.get("service", ""),
        "auth": cfg.get("auth", {}),
        "entities": cfg.get("entities", {}),
    }


@app.get("/api/mcp")
async def list_mcp_servers(request: Request):
    """List all MCP servers from mcp.json with connection status."""
    _verify_token(request)
    servers = mcp_store.load()
    return {
        "servers": [_mcp_server_entry(n, c) for n, c in servers.items()
                    if isinstance(c, dict)]
    }


@app.get("/api/mcp/{name}")
async def get_mcp_server(request: Request, name: str):
    """Get a single MCP server config."""
    _verify_token(request)
    cfg = mcp_store.get(name)
    if cfg is None:
        raise HTTPException(404, f"MCP server not found: {name}")
    return _mcp_server_entry(name, cfg)


@app.put("/api/mcp/{name}")
async def update_mcp_server(request: Request, name: str):
    """Create or update an MCP server entry in mcp.json."""
    _verify_token(request)
    body = await request.json()
    body.pop("name", None)
    body.pop("connected", None)
    body.pop("tools", None)
    mcp_logger.info("MCP server updated: %s (type=%s)", name, body.get("type", "stdio"))
    mcp_store.put(name, body)
    return {"status": "saved", "name": name}


@app.post("/api/mcp")
async def create_mcp_server(request: Request):
    """Create a new MCP server entry in mcp.json."""
    _verify_token(request)
    body = await request.json()
    name = body.pop("name", "").strip()
    body.pop("connected", None)
    body.pop("tools", None)
    if not name:
        raise HTTPException(400, "name is required")
    if mcp_store.get(name) is not None:
        raise HTTPException(409, f"MCP server already exists: {name}")
    mcp_logger.info("MCP server created: %s (type=%s)", name, body.get("type", "stdio"))
    mcp_store.put(name, body)
    return {"status": "created", "name": name}


@app.delete("/api/mcp/{name}")
async def delete_mcp_server(request: Request, name: str):
    """Remove an MCP server entry from mcp.json."""
    _verify_token(request)
    if not mcp_store.remove(name):
        raise HTTPException(404, f"MCP server not found: {name}")
    mcp_logger.info("MCP server deleted: %s", name)
    return {"status": "deleted", "name": name}


class ODataDiscoverRequest(BaseModel):
    url: str
    service: str = ""
    username: str = ""
    password: str = ""
    ssl_verify: bool = False   # default off for corporate environments


@app.post("/api/mcp/odata/discover")
async def odata_discover(request: Request, body: ODataDiscoverRequest):
    """Fetch OData $metadata and return parsed EntitySet list."""
    _verify_token(request)
    import httpx as _httpx
    from xml.etree import ElementTree as ET
    from gyrfalcon.net import get_ssl_verify, get_proxy_url

    base = body.url.rstrip("/")
    svc = body.service.strip("/")
    metadata_url = f"{base}/{svc}/$metadata" if svc else f"{base}/$metadata"

    auth = (body.username, body.password) if body.username else None
    headers = {"Accept": "application/xml"}
    ssl_verify = get_ssl_verify()

    resp = None
    last_error: str = ""

    attempts = [{"trust_env": False, "proxy": None, "label": "direct"}]
    proxy_url = get_proxy_url()
    if proxy_url:
        attempts.append({"trust_env": False, "proxy": proxy_url, "label": f"proxy({proxy_url})"})

    for attempt in attempts:
        auth_preview = f"{body.username}:***" if body.username else "(none)"
        mcp_logger.debug(
            "--- OData Discover REQUEST --------------------------------\n"
            "  attempt    : %s\n"
            "  url        : %s\n"
            "  method     : GET\n"
            "  headers    : %s\n"
            "  auth       : %s\n"
            "  ssl_verify : %s\n"
            "  proxy      : %s\n"
            "----------------------------------------------------------",
            attempt["label"], metadata_url, headers, auth_preview, ssl_verify, attempt["proxy"] or "(none)",
        )
        try:
            async with _httpx.AsyncClient(
                verify=ssl_verify,
                trust_env=attempt["trust_env"],
                proxy=attempt["proxy"],
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                resp = await client.get(metadata_url, auth=auth, headers=headers)

                mcp_logger.debug(
                    "--- OData Discover RESPONSE --------------------------------\n"
                    "  attempt     : %s\n"
                    "  status      : %s\n"
                    "  url (final) : %s\n"
                    "  response-headers: %s\n"
                    "------------------------------------------------------------",
                    attempt["label"], resp.status_code, str(resp.url), dict(resp.headers),
                )
                resp.raise_for_status()
                break
        except _httpx.HTTPStatusError as e:
            mcp_logger.debug("OData HTTP error %s: %s", e.response.status_code, e.response.text[:300])
            raise HTTPException(
                e.response.status_code,
                f"OData metadata error ({attempt['label']}): {e.response.text[:400]}"
            )
        except Exception as e:
            last_error = f"{attempt['label']}: {type(e).__name__}: {e}"
            mcp_logger.warning(f"OData discover attempt failed -- {last_error}")
            resp = None

    if resp is None:
        raise HTTPException(502, f"Could not reach OData endpoint. Attempts: {last_error}")

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        raise HTTPException(502, f"Invalid XML in metadata response: {e}\nFirst 200 chars: {resp.text[:200]}")

    entity_type_props: dict[str, list[str]] = {}
    entity_type_keys: dict[str, list[str]] = {}
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "EntityType":
            et_name = elem.get("Name", "")
            if not et_name:
                continue
            props, keys = [], []
            for child in elem:
                ctag = child.tag.split("}")[-1]
                if ctag == "Property":
                    props.append(child.get("Name", ""))
                elif ctag == "PropertyRef":
                    keys.append(child.get("Name", ""))
                elif ctag == "Key":
                    for kref in child:
                        kname = kref.get("Name", "")
                        if kname:
                            keys.append(kname)
            entity_type_props[et_name] = props[:15]
            entity_type_keys[et_name] = keys

    entities = []
    seen: set[str] = set()
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "EntitySet":
            eset_name = elem.get("Name", "")
            if not eset_name or eset_name in seen:
                continue
            seen.add(eset_name)
            et_ref = elem.get("EntityType", "").split(".")[-1]
            entities.append({
                "name": eset_name,
                "entity_type": et_ref or eset_name,
                "key_properties": entity_type_keys.get(et_ref, []),
                "properties": entity_type_props.get(et_ref, []),
            })

    entities.sort(key=lambda e: e["name"])

    # ── Save full response as formatted HTML ──────────────────────────────────
    try:
        html_path = _save_odata_html_report(
            metadata_url=metadata_url,
            service=body.service,
            raw_xml=resp.text,
            entities=entities,
            status_code=resp.status_code,
            response_headers=dict(resp.headers),
        )
        mcp_logger.info("OData discover complete: %d entities. Full response saved -> %s", len(entities), html_path)
    except Exception as e:
        mcp_logger.warning("Could not save OData HTML report: %s", e)

    return {
        "metadata_url": metadata_url,
        "entity_count": len(entities),
        "entities": entities,
    }


def _save_odata_html_report(
    metadata_url: str,
    service: str,
    raw_xml: str,
    entities: list[dict],
    status_code: int,
    response_headers: dict,
) -> str:
    """Render a pretty HTML report for the OData discover response and write to mcp/odata/."""
    import html as _html
    from datetime import datetime
    from gyrfalcon.gyrfalcon_constants import get_mcp_dir
    import xml.dom.minidom as _minidom

    # Pretty-print XML
    try:
        pretty_xml = _minidom.parseString(raw_xml).toprettyxml(indent="  ")
        # Remove the redundant first XML declaration line that minidom adds
        lines = pretty_xml.splitlines()
        pretty_xml = "\n".join(lines[1:] if lines[0].startswith("<?xml") else lines)
    except Exception:
        pretty_xml = raw_xml

    svc_slug = (service or "metadata").replace("/", "_").replace(".", "_")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = get_mcp_dir() / "odata"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"{svc_slug}_{ts}.html"

    # Build entity table rows
    entity_rows = ""
    for e in entities:
        keys_html = ", ".join(f"<code>{_html.escape(k)}</code>" for k in e.get("key_properties", []))
        props_html = " ".join(f"<span class='prop'>{_html.escape(p)}</span>" for p in e.get("properties", []))
        entity_rows += f"""
        <tr>
          <td class='name'>{_html.escape(e['name'])}</td>
          <td class='type'>{_html.escape(e.get('entity_type',''))}</td>
          <td>{keys_html or '<span class="muted">—</span>'}</td>
          <td class='props'>{props_html or '<span class="muted">—</span>'}</td>
        </tr>"""

    # Build response header rows
    header_rows = "".join(
        f"<tr><td class='hk'>{_html.escape(k)}</td><td>{_html.escape(str(v))}</td></tr>"
        for k, v in response_headers.items()
    )

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>OData Discover: {_html.escape(svc_slug)}</title>
<style>
  :root {{
    --bg:#09090b; --card:#111113; --border:rgba(255,255,255,0.07);
    --fg:#fafafa; --muted:#71717a; --primary:#FF6012; --blue:#456DE6;
    --green:#22c55e; --code-bg:#1e1e23;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--fg); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; font-size:14px; line-height:1.5; padding:24px; }}
  h1 {{ font-size:20px; font-weight:700; color:var(--primary); margin-bottom:4px; }}
  h2 {{ font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--muted); margin:24px 0 8px; }}
  .meta {{ font-size:12px; color:var(--muted); margin-bottom:20px; }}
  .meta strong {{ color:var(--fg); }}
  .badge {{ display:inline-flex; align-items:center; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:600;
            background:rgba(34,197,94,.15); color:var(--green); margin-left:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th {{ text-align:left; padding:8px 10px; font-size:11px; font-weight:700; text-transform:uppercase;
        letter-spacing:.07em; color:var(--muted); border-bottom:1px solid var(--border);
        background:var(--card); }}
  td {{ padding:7px 10px; border-bottom:1px solid var(--border); vertical-align:top; }}
  tr:hover td {{ background:rgba(255,255,255,.02); }}
  td.name {{ font-weight:600; color:var(--fg); font-family:monospace; white-space:nowrap; }}
  td.type {{ color:var(--muted); font-family:monospace; font-size:12px; white-space:nowrap; }}
  td.hk  {{ color:var(--muted); font-family:monospace; font-size:12px; white-space:nowrap; padding-right:20px; }}
  code   {{ background:var(--code-bg); border-radius:3px; padding:1px 5px; font-family:monospace; font-size:12px; color:var(--blue); }}
  .prop  {{ display:inline-block; background:var(--code-bg); border-radius:3px; padding:1px 5px; margin:1px 2px;
            font-family:monospace; font-size:11px; color:var(--muted); }}
  .muted {{ color:var(--muted); font-size:12px; }}
  .xml-wrap {{ background:var(--code-bg); border:1px solid var(--border); border-radius:6px; padding:16px;
               overflow:auto; max-height:600px; }}
  pre {{ font-family:"Consolas","Courier New",monospace; font-size:12px; line-height:1.6;
         white-space:pre; color:#a9b1d6; }}
  .section {{ background:var(--card); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:20px; }}
  .url {{ font-family:monospace; font-size:12px; color:var(--blue); word-break:break-all; }}
</style>
</head>
<body>

<h1>OData Discover Report</h1>
<div class="meta">
  <strong>Service:</strong> {_html.escape(svc_slug)} &nbsp;
  <strong>Status:</strong> {status_code}
  <span class="badge">&#10003; {len(entities)} entities</span><br/>
  <strong>URL:</strong> <span class="url">{_html.escape(metadata_url)}</span><br/>
  <strong>Generated:</strong> {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
</div>

<div class="section">
  <h2>Entity Sets ({len(entities)})</h2>
  <table>
    <thead><tr><th>Entity Set</th><th>Entity Type</th><th>Key Properties</th><th>Properties (first 15)</th></tr></thead>
    <tbody>{entity_rows}</tbody>
  </table>
</div>

<div class="section">
  <h2>Response Headers</h2>
  <table>
    <thead><tr><th>Header</th><th>Value</th></tr></thead>
    <tbody>{header_rows}</tbody>
  </table>
</div>

<div class="section">
  <h2>Raw $metadata XML ({len(raw_xml):,} bytes)</h2>
  <div class="xml-wrap"><pre>{_html.escape(pretty_xml)}</pre></div>
</div>

</body>
</html>"""

    out_file.write_text(html_content, encoding="utf-8")
    return str(out_file)


class OpenAPIDiscoverRequest(BaseModel):
    url: str
    auth_type: str = "none"          # active auth type: none | basic | apikey | oauth
    api_key: str = ""
    api_key_header: str = "Authorization"
    username: str = ""
    password: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url: str = ""
    oauth_scope: str = ""
    ssl_verify: bool = False


def resolve_openapi_auth(auth: dict) -> dict:
    """Extract active credentials from a MultiAuth or legacy flat auth dict.

    Returns a flat dict: {auth_type, username, password, api_key, api_key_header, oauth_*}
    so callers don't need to know which format is stored.
    """
    if not auth:
        return {"auth_type": "none", "username": "", "password": "", "api_key": "", "api_key_header": "Authorization",
                "oauth_client_id": "", "oauth_client_secret": "", "oauth_token_url": "", "oauth_scope": ""}

    # New MultiAuth format: has sub-keys none/basic/apikey/oauth
    if "none" in auth or "oauth" in auth:
        active = auth.get("type", "none")
        basic  = auth.get("basic",  {}) or {}
        apikey = auth.get("apikey", {}) or {}
        oauth  = auth.get("oauth",  {}) or {}
        return {
            "auth_type":          active,
            "username":           basic.get("username", ""),
            "password":           basic.get("password", ""),
            "api_key":            apikey.get("api_key", ""),
            "api_key_header":     apikey.get("api_key_header", "Authorization"),
            "oauth_client_id":    oauth.get("client_id", ""),
            "oauth_client_secret":oauth.get("client_secret", ""),
            "oauth_token_url":    oauth.get("token_url", ""),
            "oauth_scope":        oauth.get("scope", ""),
        }

    # Legacy flat format: { type, username, password, api_key, api_key_header }
    return {
        "auth_type":          auth.get("type", "none"),
        "username":           auth.get("username", ""),
        "password":           auth.get("password", ""),
        "api_key":            auth.get("api_key", ""),
        "api_key_header":     auth.get("api_key_header", "Authorization"),
        "oauth_client_id":    "",
        "oauth_client_secret":"",
        "oauth_token_url":    "",
        "oauth_scope":        "",
    }


@app.post("/api/mcp/openapi/discover")
async def openapi_discover(request: Request, body: OpenAPIDiscoverRequest):
    """Fetch an OpenAPI spec and return all operations."""
    _verify_token(request)
    import httpx as _httpx
    import json as _json
    from gyrfalcon.net import get_ssl_verify, get_proxy_url
    ssl_verify = get_ssl_verify()
    headers: dict = {"Accept": "application/json, application/yaml, text/yaml"}

    auth_type = body.auth_type or "none"
    if auth_type == "apikey" and body.api_key:
        headers[body.api_key_header] = body.api_key
    elif auth_type == "oauth" and body.oauth_token_url:
        try:
            async with _httpx.AsyncClient(verify=ssl_verify, trust_env=False, timeout=15.0) as cl:
                token_resp = await cl.post(
                    body.oauth_token_url,
                    data={"grant_type": "client_credentials", "client_id": body.oauth_client_id,
                          "client_secret": body.oauth_client_secret, "scope": body.oauth_scope},
                )
                token_resp.raise_for_status()
                bearer = token_resp.json().get("access_token", "")
                if bearer:
                    headers["Authorization"] = f"Bearer {bearer}"
        except Exception as e:
            raise HTTPException(502, f"OAuth token fetch failed: {e}")

    auth = (body.username, body.password) if (auth_type == "basic" and body.username) else None
    auth = (body.username, body.password) if body.username else None

    attempts = [{"trust_env": False, "proxy": None, "label": "direct"}]
    proxy_url = get_proxy_url()
    if proxy_url:
        attempts.append({"trust_env": False, "proxy": proxy_url, "label": f"proxy({proxy_url})"})

    resp = None
    last_error = ""
    for attempt in attempts:
        mcp_logger.debug("OpenAPI discover REQUEST: %s via %s", body.url, attempt["label"])
        try:
            async with _httpx.AsyncClient(
                verify=ssl_verify, trust_env=False,
                proxy=attempt["proxy"], timeout=30.0, follow_redirects=True,
            ) as client:
                resp = await client.get(body.url, headers=headers, auth=auth)
                mcp_logger.debug("OpenAPI discover RESPONSE: status=%s url=%s", resp.status_code, str(resp.url))
                resp.raise_for_status()
                break
        except _httpx.HTTPStatusError as e:
            raise HTTPException(e.response.status_code,
                                f"OpenAPI fetch error: {e.response.text[:300]}")
        except Exception as e:
            last_error = f"{attempt['label']}: {type(e).__name__}: {e}"
            mcp_logger.warning("OpenAPI discover attempt failed -- %s", last_error)
            resp = None

    if resp is None:
        raise HTTPException(502, f"Could not reach spec URL. {last_error}")

    # Parse JSON or YAML
    content_type = resp.headers.get("content-type", "")
    try:
        if "yaml" in content_type or body.url.endswith((".yaml", ".yml")):
            try:
                import yaml as _yaml
                spec = _yaml.safe_load(resp.text)
            except ImportError:
                raise HTTPException(502, "YAML spec requires PyYAML: pip install pyyaml")
        else:
            spec = _json.loads(resp.text)
    except Exception as e:
        raise HTTPException(502, f"Could not parse OpenAPI spec: {e}")

    if not isinstance(spec, dict):
        raise HTTPException(502, "Invalid OpenAPI spec: root must be an object")

    # Extract operations from paths
    operations = []
    paths = spec.get("paths") or {}
    HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(op, dict):
                continue
            op_id = op.get("operationId") or f"{method.upper()}_{path.strip('/').replace('/', '_')}"
            operations.append({
                "operation_id": op_id,
                "method":       method.upper(),
                "path":         path,
                "summary":      op.get("summary") or op.get("description") or "",
                "tags":         op.get("tags") or [],
            })

    operations.sort(key=lambda o: (o["tags"][0] if o["tags"] else "zzz", o["path"], o["method"]))

    info = spec.get("info") or {}
    mcp_logger.info("OpenAPI discover complete: %d operations from %s v%s",
                    len(operations), info.get("title", "?"), info.get("version", "?"))

    return {
        "spec_url": str(resp.url),
        "title":    info.get("title", ""),
        "version":  info.get("version", ""),
        "operation_count": len(operations),
        "operations": operations,
    }


# --- Toolsets ---

@app.get("/api/tools/toolsets")
async def list_toolsets_api(request: Request):
    _verify_token(request)
    return {"toolsets": TOOLSETS}


# --- Logs ---

@app.get("/api/logs")
@app.get("/api/logs")
async def get_logs(request: Request, file: str = "agent.log", lines: int = 200, limit: int = 0, level: str = ""):
    _verify_token(request)
    import re as _re
    from gyrfalcon.gyrfalcon_constants import get_logs_dir
    log_file = get_logs_dir() / file
    if not log_file.exists():
        return {"logs": [], "lines": []}

    content = log_file.read_text(errors="replace")
    all_lines = [l for l in content.split("\n") if l.strip()]

    n = limit if limit > 0 else lines

    # Parse "2026-07-22 07:33:40 [LEVEL] [session] module: message"
    _pat = _re.compile(
        r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (?:\[[^\]]*\] )?([\w.]+): (.*)$'
    )
    _order = {"TRACE": 0, "DEBUG": 10, "STATEMENT": 15, "INFO": 20,
               "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
    min_level = _order.get(level.upper(), 0) if level else 0

    parsed = []
    # Read from tail so we have enough entries after filtering
    for raw in all_lines[-(max(n * 4, 1000)):]:
        m = _pat.match(raw)
        if not m:
            continue
        entry_level = m.group(2)
        if min_level and _order.get(entry_level, 0) < min_level:
            continue
        parsed.append({
            "timestamp": m.group(1),
            "level":     entry_level,
            "module":    m.group(3),
            "message":   m.group(4),
        })

    return {"logs": parsed[-n:], "lines": all_lines[-n:]}


# --- Applications ---

import uuid as _uuid_mod


def _load_applications() -> list[dict]:
    from gyrfalcon.gyrfalcon_constants import get_applications_file
    p = get_applications_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("applications", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_applications(apps: list[dict]) -> None:
    from gyrfalcon.gyrfalcon_constants import get_applications_file
    p = get_applications_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"applications": apps}, indent=2), encoding="utf-8")


class ApplicationRequest(BaseModel):
    name: str
    description: str = ""
    enabled: bool = True
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    tags: list[str] = []
    version: str = "1.0.0"
    source_sessions: list[str] = []
    config: dict = {}


class ToggleRequest(BaseModel):
    enabled: bool


@app.get("/api/applications")
async def list_applications(request: Request):
    _verify_token(request)
    return {"applications": _load_applications()}


@app.post("/api/applications")
async def create_application(request: Request, body: ApplicationRequest):
    _verify_token(request)
    apps = _load_applications()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = {
        "id": str(_uuid_mod.uuid4()),
        "name": body.name,
        "description": body.description,
        "enabled": body.enabled,
        "command": body.command,
        "args": body.args,
        "env": body.env,
        "tags": body.tags,
        "version": body.version,
        "source_sessions": body.source_sessions,
        "config": body.config,
        "created_at": now,
        "updated_at": now,
    }
    apps.append(entry)
    _save_applications(apps)
    return entry


@app.get("/api/applications/{app_id}")
async def get_application(request: Request, app_id: str):
    _verify_token(request)
    for a in _load_applications():
        if a["id"] == app_id:
            return a
    raise HTTPException(404, "Application not found")


@app.put("/api/applications/{app_id}")
async def update_application(request: Request, app_id: str, body: ApplicationRequest):
    _verify_token(request)
    apps = _load_applications()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, a in enumerate(apps):
        if a["id"] == app_id:
            apps[i] = {
                **a,
                "name": body.name,
                "description": body.description,
                "enabled": body.enabled,
                "command": body.command,
                "args": body.args,
                "env": body.env,
                "tags": body.tags,
                "version": body.version,
                "source_sessions": body.source_sessions,
                "config": body.config,
                "updated_at": now,
            }
            _save_applications(apps)
            return apps[i]
    raise HTTPException(404, "Application not found")


@app.delete("/api/applications/{app_id}")
async def delete_application(request: Request, app_id: str):
    _verify_token(request)
    apps = _load_applications()
    new_apps = [a for a in apps if a["id"] != app_id]
    if len(new_apps) == len(apps):
        raise HTTPException(404, "Application not found")
    _save_applications(new_apps)
    return {"ok": True}


@app.post("/api/applications/{app_id}/toggle")
async def toggle_application(request: Request, app_id: str, body: ToggleRequest):
    _verify_token(request)
    apps = _load_applications()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, a in enumerate(apps):
        if a["id"] == app_id:
            apps[i] = {**a, "enabled": body.enabled, "updated_at": now}
            _save_applications(apps)
            return apps[i]
    raise HTTPException(404, "Application not found")


# --- Security: Service Accounts (Administration > Security) ---

class ServiceAccountCreate(BaseModel):
    name: str
    scopes: list[str] = []


@app.get("/api/security/service-accounts")
async def list_service_accounts(request: Request):
    _verify_token(request)
    from gyrfalcon.security import load_service_accounts, public_service_account
    return {"service_accounts": [public_service_account(a) for a in load_service_accounts()]}


@app.post("/api/security/service-accounts")
async def create_service_account_endpoint(request: Request, body: ServiceAccountCreate):
    """The response's `client_secret` is shown to the caller exactly once —
    it is never recoverable again, only rotated."""
    _verify_token(request)
    from gyrfalcon.security import create_service_account
    account, client_secret = create_service_account(body.name, body.scopes)
    return {**account, "client_secret": client_secret}


@app.post("/api/security/service-accounts/{account_id}/rotate")
async def rotate_service_account_endpoint(request: Request, account_id: str):
    _verify_token(request)
    from gyrfalcon.security import rotate_service_account_secret
    client_secret = rotate_service_account_secret(account_id)
    if client_secret is None:
        raise HTTPException(404, "Service account not found")
    return {"client_secret": client_secret}


@app.post("/api/security/service-accounts/{account_id}/toggle")
async def toggle_service_account_endpoint(request: Request, account_id: str, body: ToggleRequest):
    _verify_token(request)
    from gyrfalcon.security import set_service_account_enabled
    account = set_service_account_enabled(account_id, body.enabled)
    if account is None:
        raise HTTPException(404, "Service account not found")
    return account


@app.delete("/api/security/service-accounts/{account_id}")
async def delete_service_account_endpoint(request: Request, account_id: str):
    _verify_token(request)
    from gyrfalcon.security import delete_service_account
    if not delete_service_account(account_id):
        raise HTTPException(404, "Service account not found")
    return {"ok": True}


class OAuthTokenRequest(BaseModel):
    grant_type: str = "client_credentials"
    client_id: str
    client_secret: str


@app.post("/api/oauth/token")
async def issue_oauth_token(body: OAuthTokenRequest):
    """The endpoint a Service Account's owning application actually calls —
    no dashboard session token required, since the whole point is letting an
    external app authenticate on its own. RFC 6749 §4.4 client-credentials
    grant, minus refresh tokens (a client just re-authenticates when the
    access token expires — it already holds the credential that grants one)."""
    if body.grant_type != "client_credentials":
        raise HTTPException(400, "Only grant_type=client_credentials is supported")
    from gyrfalcon.security import issue_access_token
    token = issue_access_token(body.client_id, body.client_secret)
    if token is None:
        raise HTTPException(401, "Invalid client credentials")
    return token


# --- Security: Secret Store (Administration > Security) ---

class SecretCreate(BaseModel):
    name: str
    description: str = ""
    value: str


class SecretUpdate(BaseModel):
    description: Optional[str] = None
    value: Optional[str] = None


@app.get("/api/security/secrets")
async def list_secrets(request: Request):
    _verify_token(request)
    from gyrfalcon.security import load_secrets, public_secret
    return {"secrets": [public_secret(s) for s in load_secrets()]}


@app.post("/api/security/secrets")
async def create_secret_endpoint(request: Request, body: SecretCreate):
    """The value is accepted but never echoed back — not here, not on GET."""
    _verify_token(request)
    from gyrfalcon.security import create_secret
    return create_secret(body.name, body.description, body.value)


@app.put("/api/security/secrets/{secret_id}")
async def update_secret_endpoint(request: Request, secret_id: str, body: SecretUpdate):
    _verify_token(request)
    from gyrfalcon.security import update_secret
    secret = update_secret(secret_id, description=body.description, value=body.value)
    if secret is None:
        raise HTTPException(404, "Secret not found")
    return secret


@app.delete("/api/security/secrets/{secret_id}")
async def delete_secret_endpoint(request: Request, secret_id: str):
    _verify_token(request)
    from gyrfalcon.security import delete_secret
    if not delete_secret(secret_id):
        raise HTTPException(404, "Secret not found")
    return {"ok": True}


# --- Agents ---

def _load_agents() -> list[dict]:
    from gyrfalcon.agents import load_agents
    return load_agents()


def _save_agents(agents: list[dict]) -> None:
    from gyrfalcon.gyrfalcon_constants import get_agents_file
    p = get_agents_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"agents": agents}, indent=2), encoding="utf-8")


def _agent_entry(a: dict) -> dict:
    """Return a sanitised agent dict (strip internal fields)."""
    return {k: v for k, v in a.items() if k != "_gateway_api_key_internal"}


class AgentCreateRequest(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    enabled: bool = True
    model: str = ""
    provider: str = ""
    max_iterations: int = 30
    mcp_servers: list[str] = []
    skills: list[str] = []
    enabled_toolsets: list[str] = []
    plugins: list[str] = []
    gateway_enabled: bool = False
    tags: list[str] = []


@app.get("/api/agents")
async def list_agents_api(request: Request):
    _verify_token(request)
    from gyrfalcon.agents import agent_config_version
    return {"agents": [{**_agent_entry(a), "flow_version": agent_config_version(a)}
                        for a in _load_agents()]}


@app.post("/api/agents")
async def create_agent_api(request: Request, body: AgentCreateRequest):
    _verify_token(request)
    agents = _load_agents()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    gw_key = secrets.token_urlsafe(24) if body.gateway_enabled else ""
    entry = {
        "id":               str(_uuid_mod.uuid4()),
        "name":             body.name,
        "description":      body.description,
        "instructions":     body.instructions,
        "enabled":          body.enabled,
        "model":            body.model,
        "provider":         body.provider,
        "max_iterations":   body.max_iterations,
        "mcp_servers":      body.mcp_servers,
        "skills":           body.skills,
        "enabled_toolsets": body.enabled_toolsets,
        "plugins":          body.plugins,
        "gateway": {
            "enabled":  body.gateway_enabled,
            "api_key":  gw_key,
        },
        "tags":       body.tags,
        "created_at": now,
        "updated_at": now,
    }
    agents.append(entry)
    _save_agents(agents)
    return _agent_entry(entry)


@app.get("/api/agents/{agent_id}")
async def get_agent_api(request: Request, agent_id: str):
    _verify_token(request)
    for a in _load_agents():
        if a["id"] == agent_id:
            return _agent_entry(a)
    raise HTTPException(404, "Agent not found")


@app.put("/api/agents/{agent_id}")
async def update_agent_api(request: Request, agent_id: str, body: AgentCreateRequest):
    _verify_token(request)
    agents = _load_agents()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, a in enumerate(agents):
        if a["id"] == agent_id:
            # Preserve existing gateway key unless regenerating
            existing_gw = a.get("gateway", {})
            gw_key = existing_gw.get("api_key", "") or (secrets.token_urlsafe(24) if body.gateway_enabled else "")
            agents[i] = {
                **a,
                "name":             body.name,
                "description":      body.description,
                "instructions":     body.instructions,
                "enabled":          body.enabled,
                "model":            body.model,
                "provider":         body.provider,
                "max_iterations":   body.max_iterations,
                "mcp_servers":      body.mcp_servers,
                "skills":           body.skills,
                "enabled_toolsets": body.enabled_toolsets,
                "plugins":          body.plugins,
                "gateway": {"enabled": body.gateway_enabled, "api_key": gw_key},
                "tags":             body.tags,
                "updated_at":       now,
            }
            _save_agents(agents)
            return _agent_entry(agents[i])
    raise HTTPException(404, "Agent not found")


@app.delete("/api/agents/{agent_id}")
async def delete_agent_api(request: Request, agent_id: str):
    _verify_token(request)
    agents = _load_agents()
    new_agents = [a for a in agents if a["id"] != agent_id]
    if len(new_agents) == len(agents):
        raise HTTPException(404, "Agent not found")
    _save_agents(new_agents)
    return {"ok": True}


@app.post("/api/agents/{agent_id}/toggle")
async def toggle_agent_api(request: Request, agent_id: str, body: ToggleRequest):
    _verify_token(request)
    agents = _load_agents()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, a in enumerate(agents):
        if a["id"] == agent_id:
            agents[i] = {**a, "enabled": body.enabled, "updated_at": now}
            _save_agents(agents)
            return _agent_entry(agents[i])
    raise HTTPException(404, "Agent not found")


@app.post("/api/agents/{agent_id}/regenerate-key")
async def regenerate_agent_key(request: Request, agent_id: str):
    _verify_token(request)
    agents = _load_agents()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i, a in enumerate(agents):
        if a["id"] == agent_id:
            new_key = secrets.token_urlsafe(24)
            agents[i] = {**a, "gateway": {**a.get("gateway", {}), "api_key": new_key}, "updated_at": now}
            _save_agents(agents)
            return {"api_key": new_key}
    raise HTTPException(404, "Agent not found")


class AgentInvokeRequest(BaseModel):
    message: str
    session_id: str | None = None


@app.post("/api/agents/{agent_id}/invoke")
async def invoke_agent_api(request: Request, agent_id: str, body: AgentInvokeRequest):
    """Invoke an agent from the dashboard (authenticated)."""
    _verify_token(request)
    return await _run_agent_invoke(agent_id, body.message, body.session_id)


@app.post("/api/agents/{agent_id}/sessions")
async def start_agent_chat_session(request: Request, agent_id: str):
    """Create an empty chat session bound to a saved Agent configuration.

    The agentic loop starts when the user sends their first chat message. The
    session's agent_id makes every turn use that Agent's saved prompt, model,
    skills, toolsets, plugins and attached MCP servers.
    """
    _verify_token(request)
    agent_cfg = next((a for a in _load_agents() if a["id"] == agent_id), None)
    if agent_cfg is None:
        raise HTTPException(404, "Agent not found")
    if not agent_cfg.get("enabled"):
        raise HTTPException(400, "Agent is disabled")

    from gyrfalcon.agents import build_agent_kwargs
    from gyrfalcon.gyrfalcon_state import SessionDB

    agent_kwargs = build_agent_kwargs(agent_cfg)
    session_id = str(_uuid_mod.uuid4())
    session_db = SessionDB()
    try:
        session_db.create_session(
            session_id=session_id,
            source="chat",
            model=agent_kwargs["model"],
            title=f"{agent_cfg['name']} · Agent session",
            agent_id=agent_id,
        )
    finally:
        session_db.close()
    return {"session_id": session_id, "agent_id": agent_id}


# --- Agent Gateway (public endpoint, API-key auth) ---

@app.post("/api/gateway/agents/{agent_id}")
async def gateway_invoke(request: Request, agent_id: str):
    """Public gateway endpoint — auth via X-Agent-Key header or api_key query param."""
    # Resolve agent
    agent_cfg = next((a for a in _load_agents() if a["id"] == agent_id), None)
    if not agent_cfg:
        raise HTTPException(404, "Agent not found")

    gw = agent_cfg.get("gateway", {})
    if not gw.get("enabled"):
        raise HTTPException(403, "Gateway not enabled for this agent")

    # Authenticate
    provided_key = (
        request.headers.get("X-Agent-Key")
        or request.query_params.get("api_key")
        or ""
    )
    expected_key = gw.get("api_key", "")
    if expected_key and provided_key != expected_key:
        raise HTTPException(401, "Invalid API key")

    body = await request.json()
    message = body.get("message", "")
    if not message:
        raise HTTPException(400, "message is required")
    session_id = body.get("session_id")

    return await _run_agent_invoke(agent_id, message, session_id)


async def _run_agent_invoke(agent_id: str, message: str, session_id: str | None) -> dict:
    """Run an agent asynchronously, return session_id immediately.

    Pre-creates the session in the DB so the UI can resume it right away,
    then runs the agent in a background thread.  Streaming deltas and status
    events are published to the module-level event bus so the WebSocket
    handler can forward them to any connected client watching this session.
    """
    agent_cfg = next((a for a in _load_agents() if a["id"] == agent_id), None)
    if not agent_cfg:
        raise HTTPException(404, "Agent not found")
    if not agent_cfg.get("enabled"):
        raise HTTPException(400, "Agent is disabled")

    import uuid as _uuid
    from gyrfalcon.gyrfalcon_state import SessionDB
    from gyrfalcon.agents import build_agent_kwargs

    sid = session_id or str(_uuid.uuid4())
    agent_kwargs = build_agent_kwargs(agent_cfg)
    plugin_manager = None
    if agent_cfg.get("plugins"):
        from gyrfalcon.plugins import PluginManager

        plugin_manager = PluginManager()
        plugin_manager.discover_and_load()
        plugin_manager = plugin_manager.for_plugins(agent_cfg["plugins"])

    # ── Pre-create session so session.resume returns immediately ─────────────
    # We just create the session row — AIAgent will write the real messages.
    # Tagging it with agent_id is what lets every later turn on this session
    # (including ones sent from the chat page, not this invoke endpoint)
    # rebuild the AIAgent from this same config instead of falling back to a
    # generic chat agent — see tui_gateway/server.py:_get_or_create_agent.
    pre_db = SessionDB()
    try:
        if not pre_db.get_session(sid):
            pre_db.create_session(
                session_id=sid, source="chat", model=agent_kwargs["model"], agent_id=agent_id,
            )
    finally:
        pre_db.close()

    # Register the run before the thread starts, so a client that resumes the
    # session the instant this endpoint responds can still subscribe.
    _agent_run_begin(sid)

    # ── Background runner ────────────────────────────────────────────────────
    def _run() -> None:
        _agent_event_publish(sid, {"method": "status.update", "params": {"state": "thinking"}})
        run_db = SessionDB()
        try:
            from gyrfalcon.run_agent import AIAgent

            def _on_delta(delta: str) -> None:
                _agent_event_publish(sid, {"method": "message.delta", "params": {"content": delta}})

            def _on_tool(tool_name: str, args: dict, status: str) -> None:
                from gyrfalcon.tools import registry as _reg
                if status == "start":
                    _agent_event_publish(sid, {"method": "tool.start", "params": {
                        "name": tool_name, "args": args,
                        "read_only": _reg.is_read_only(tool_name),
                        "write": not _reg.is_read_only(tool_name),
                    }})
                elif status == "complete":
                    _agent_event_publish(sid, {"method": "tool.complete", "params": {"name": tool_name}})

            # session_db is what makes the run durable: AIAgent gates every
            # write on it, so without it the prompt and the reply are streamed
            # and then lost, and resuming the session shows an empty chat.
            agent = AIAgent(
                **agent_kwargs,
                quiet_mode=False,
                skip_memory=False,
                platform="chat",
                session_id=sid,
                session_db=run_db,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool,
                plugin_manager=plugin_manager,
            )
            result = agent.run_conversation(user_message=message)
            response = result.get("final_response", "")
            # Retire the backlog before announcing completion. The reply is now
            # in the DB, so a client that subscribes from here on must render it
            # from session.resume only — replaying it as well would show the
            # answer twice, since the client appends on message.complete.
            _agent_run_end(sid)
            _agent_event_publish(sid, {
                "method": "message.complete",
                "params": {"content": response, "session_id": sid},
            })
            logger.info(f"Agent {agent_id} completed -> session {sid}")
        except Exception as e:
            logger.error(f"Agent {agent_id} invocation failed: {e}", exc_info=True)
            _agent_event_publish(sid, {"method": "error", "params": {"message": str(e)}})
        finally:
            _agent_event_publish(sid, {"method": "status.update", "params": {"state": "idle"}})
            _agent_event_publish(sid, {"method": "_agent_done", "params": {}})
            _agent_run_end(sid)
            run_db.close()

    import threading as _threading
    _threading.Thread(target=_run, daemon=True, name=f"agent-{agent_id[:6]}").start()
    return {"status": "started", "session_id": sid, "agent_id": agent_id}


# --- WebSocket: JSON-RPC Gateway ---

@app.websocket("/api/ws")
async def websocket_gateway(ws: WebSocket):
    token = ws.query_params.get("token")
    # Debug: log first/last 4 chars of tokens to compare
    token_preview = f"{token[:4]}...{token[-4:]}" if token and len(token) > 8 else token
    expected_preview = f"{_session_token[:4]}...{_session_token[-4:]}"
    logger.info(f"WebSocket auth - received: {token_preview}, expected: {expected_preview}, match: {token == _session_token}")
    
    if token != _session_token:
        logger.warning(f"WebSocket auth failed - token mismatch, rejecting with 4001")
        await ws.close(code=4001)
        return

    await ws.accept()
    logger.info("WebSocket connection accepted")

    from tui_gateway.transport import WebSocketTransport
    from tui_gateway.server import TUIGatewayServer

    # Create WebSocket tracer for this connection
    ws_tracer = WebSocketTracer()

    transport = WebSocketTransport(ws)
    server = TUIGatewayServer(transport, ws_tracer=ws_tracer)

    # Every frame on this socket goes through the transport's single writer.
    # Awaiting ws.send_* from more than one task interleaves writes into the
    # connection's shared permessage-deflate compressor and corrupts the stream.
    # flush=False is required here: these callers run on the event loop thread,
    # and blocking for the drain task from inside the loop would deadlock it.
    def _ws_send(payload: dict) -> None:
        transport.send(json.dumps(payload), flush=False)

    # Send ready event
    skin_data = {"name": "default", "agent_name": get_app_name()}
    with trace_span("ws.send.gateway.ready", {"ws.method": "gateway.ready"}):
        _ws_send({"jsonrpc": "2.0", "method": "gateway.ready", "params": {"skin": skin_data}})
    logger.info("Sent gateway.ready to client")

    # ── Agent event bus forwarder ────────────────────────────────────────────
    # When a background agent is running for a session that this WS client
    # has resumed, we forward events from the module-level event bus to the
    # connected WebSocket in real time.
    import asyncio as _asyncio_ws
    _active_agent_queues: dict[str, _asyncio_ws.Queue] = {}
    _agent_forward_tasks: list[_asyncio_ws.Task] = []
    _loop = _asyncio_ws.get_running_loop()

    async def _forward_agent_events(session_id: str, queue: _asyncio_ws.Queue) -> None:
        """Drain agent events from the queue and forward them to this WS."""
        try:
            while True:
                event = await queue.get()
                method = event.get("method", "")
                params = event.get("params", {})
                if method == "_agent_done":
                    break
                try:
                    _ws_send({"jsonrpc": "2.0", "method": method, "params": params})
                except Exception:
                    break
        finally:
            _agent_event_unsubscribe(session_id, queue)
            _active_agent_queues.pop(session_id, None)

    def _maybe_subscribe_agent(session_id: str) -> None:
        """Subscribe this WS to agent events for session_id if a run is in flight.

        Runs on the event loop thread, so replaying the backlog here — before
        yielding — keeps missed events ahead of live ones in the queue.
        """
        if session_id in _active_agent_queues:
            return
        with _agent_event_lock:
            if session_id not in _agent_event_backlog:
                return   # No agent run in flight; session.resume already
                         # returned whatever this session contains.
        q: _asyncio_ws.Queue = _asyncio_ws.Queue()
        _active_agent_queues[session_id] = q
        for missed in _agent_event_subscribe(session_id, q, _loop):
            q.put_nowait(missed)
        task = _loop.create_task(_forward_agent_events(session_id, q))
        _agent_forward_tasks.append(task)
        logger.info(f"WS subscribed to agent events for session {session_id[:8]}")

    # Monkey-patch session.resume to also subscribe to agent events
    _orig_handle = server._handle_message

    def _patched_handle(data: str) -> None:
        _orig_handle(data)
        try:
            msg = json.loads(data)
            if msg.get("method") == "session.resume":
                sid = (msg.get("params") or {}).get("session_id", "")
                if sid:
                    _maybe_subscribe_agent(sid)
        except Exception:
            pass

    server._handle_message = _patched_handle

    try:
        while True:
            data = await ws.receive_text()
            logger.debug(f"WS received: {data[:100]}...")
            try:
                msg = json.loads(data)
                method = msg.get("method", "unknown")
                with trace_span(f"ws.receive.{method}", {"ws.method": method}):
                    server._handle_message(data)
            except json.JSONDecodeError:
                server._handle_message(data)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        # Cancel pending forwarder tasks
        for task in _agent_forward_tasks:
            task.cancel()
        server.stop()
        logger.info("WebSocket connection closed")


# Visual graph authoring and runtime share SQLAlchemy-backed stores.
def _graph_store():
    from gyrfalcon.flow import sample_activities  # noqa: F401 - register built-ins
    from gyrfalcon.flow.graphs import GraphStore
    from gyrfalcon.flow.registry import discover_flows

    discover_flows()
    return GraphStore()


class GraphCreate(BaseModel):
    name: str
    draft: dict


class GraphDraft(BaseModel):
    draft: dict


class GraphRename(BaseModel):
    name: str


class GraphEnabled(BaseModel):
    enabled: bool



def _require_flow_designer(request: Request):
    principal = _verify_token(request)
    if not principal.has_role("admin", "system_admin", "app_developer", "operator"):
        raise HTTPException(403, "Administrator or developer role required")
    return principal


@app.get("/api/flow/graphs/activities")
async def list_graph_activities(request: Request):
    _require_flow_designer(request)
    from gyrfalcon.flow import sample_activities  # noqa: F401
    from gyrfalcon.flow.registry import discover_flows, list_activities

    discover_flows()
    return {"activities": list_activities()}


@app.get("/api/flow/graphs/activity-modules")
async def list_graph_activity_modules(request: Request):
    _require_flow_designer(request)
    from gyrfalcon.flow.registry import list_activity_modules
    return {"modules": list_activity_modules()}


@app.get("/api/flow/graphs/activity-modules/{name}")
async def get_graph_activity_module(request: Request, name: str):
    _require_flow_designer(request)
    from gyrfalcon.flow.registry import get_activity_module
    try:
        return {"name": name, "content": get_activity_module(name)}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.put("/api/flow/graphs/activity-modules/{name}")
async def save_graph_activity_module(request: Request, name: str, body: FlowSourceUpdate):
    _require_flow_designer(request)
    from gyrfalcon.flow.registry import list_activities, save_activity_module
    try:
        save_activity_module(name, body.content)
    except (ValueError, SyntaxError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"activities": list_activities()}


@app.get("/api/flow/graphs")
async def list_flow_graphs(request: Request):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        return {"graphs": [{**row, "published_version": store.latest_version(row["id"])}
                           for row in store.list()]}
    finally:
        store.close()


@app.post("/api/flow/graphs")
async def create_flow_graph(request: Request, body: GraphCreate):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        return {"id": store.create(body.name.strip(), body.draft)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.post("/api/flow/graphs/sample")
async def create_sample_flow_graph(request: Request):
    _require_flow_designer(request)
    from gyrfalcon.flow.sample_activities import sample_graph

    store = _graph_store()
    try:
        existing = next((row for row in store.list() if row["name"] == "Hello sample"), None)
        if existing:
            return {"id": existing["id"]}
        definition_id = store.create("Hello sample", sample_graph())
        store.publish(definition_id)
        return {"id": definition_id}
    finally:
        store.close()


@app.post("/api/flow/graphs/complex-sample/run")
async def run_complex_sample_flow(request: Request):
    """Publish and queue the deterministic ten-Activity visual runtime demo."""
    principal = _require_flow_designer(request)
    from gyrfalcon.flow import demo_activities  # noqa: F401

    store = _graph_store()
    try:
        definition = next((row for row in store.list()
                           if row["name"] == "Order Fulfillment Demo"), None)
        if definition is None:
            from gyrfalcon.flow.demo_activities import sample_graph
            definition_id = store.create("Order Fulfillment Demo", sample_graph())
        else:
            definition_id = definition["id"]
        version = store.latest_version(definition_id)
        if version is None:
            version = store.publish(definition_id)
        runtime = store._store
        short_name = f"order-demo-{definition_id[:8]}"
        deployment = next((item for item in runtime.list_deployments()
                           if item["short_name"] == short_name), None)
        if deployment is None:
            deployment = runtime.create_deployment(
                name="Order Fulfillment Demo", short_name=short_name,
                definition_id=definition_id, version=version,
                user_id=principal.user_id, parameters={}, input_schema={}, paused=False,
            )
        from gyrfalcon.flow.demo_activities import SAMPLE_INPUTS
        run = runtime.create_run(
            definition_id, deployment["version"], SAMPLE_INPUTS, "demo",
            user_id=principal.user_id, deployment_id=deployment["id"],
            trigger_ref=short_name,
        )
        runtime.record_event(run["id"], "flow.queued", {"trigger": "demo"})
        return {"definition_id": definition_id, "version": version,
                "deployment_id": deployment["id"], "run_id": run["id"],
                "state": run["state"]}
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.get("/api/flow/graphs/{definition_id}")
async def get_flow_graph(request: Request, definition_id: str):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        row = store.get(definition_id)
        if row is None:
            raise HTTPException(404, "Graph not found")
        return {**row, "published_version": store.latest_version(definition_id)}
    finally:
        store.close()


@app.get("/api/flow/graphs/{definition_id}/attrs")
async def get_flow_graph_attrs(request: Request, definition_id: str, version: int = 0):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        return {"attributes": store.attributes(definition_id, version)}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    finally:
        store.close()


@app.put("/api/flow/graphs/{definition_id}/draft")
async def save_flow_graph_draft(request: Request, definition_id: str, body: GraphDraft):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        store.save_draft(definition_id, body.draft)
        return {"status": "saved"}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.put("/api/flow/graphs/{definition_id}/name")
async def rename_flow_graph(request: Request, definition_id: str, body: GraphRename):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        store.rename(definition_id, body.name)
        return {"status": "renamed"}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.put("/api/flow/graphs/{definition_id}/enabled")
async def set_flow_graph_enabled(request: Request, definition_id: str, body: GraphEnabled):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        store.set_enabled(definition_id, body.enabled)
        return {"status": "updated", "enabled": body.enabled}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    finally:
        store.close()


@app.delete("/api/flow/graphs/{definition_id}")
async def delete_flow_graph(request: Request, definition_id: str):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        store.delete(definition_id)
        return {"status": "deleted"}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    finally:
        store.close()


@app.post("/api/flow/graphs/{definition_id}/publish")
async def publish_flow_graph(request: Request, definition_id: str):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        return {"version": store.publish(definition_id)}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.post("/api/flow/graphs/{definition_id}/unpublish")
async def unpublish_flow_graph(request: Request, definition_id: str):
    _require_flow_designer(request)
    store = _graph_store()
    try:
        store.unpublish(definition_id)
        return {"status": "unpublished"}
    except KeyError as exc:
        raise HTTPException(404, "Graph not found") from exc
    finally:
        store.close()


class VisualDeploymentCreate(BaseModel):
    name: str
    short_name: str
    definition_id: str
    version: int | None = None
    schedule: str | None = None
    input_schema: dict = {}
    parameters: dict = {}
    allowed_service_account_ids: list[str] = []
    paused: bool = False


class VisualRunRequest(BaseModel):
    inputs: dict = {}
    caller_key: str | None = None


class FlowResponseRequest(BaseModel):
    value: Any = None
    transient: str | None = None


class FlowNotificationTemplate(BaseModel):
    name: str
    channel: str = "dashboard"
    recipient_kind: str = "user"
    recipient_ref: str
    agent_id: str | None = None
    subject_template: str | None = None
    body_template: str | None = None
    config: dict = {}


def _visual_store(tenant_id: str | None = None):
    from gyrfalcon.flow.runtime_store import FlowRuntimeStore
    from gyrfalcon.identity import require_principal

    principal = require_principal()
    return FlowRuntimeStore(tenant_id=tenant_id or principal.tenant_id, migrate=True)


def _schedule_next_at(schedule: str | None) -> float | None:
    if not schedule:
        return None
    from datetime import datetime
    from gyrfalcon.scheduler import next_run_iso, parse_schedule

    value = next_run_iso(parse_schedule(schedule, use_llm=False))
    return datetime.fromisoformat(value).timestamp() if value else None


def _merge_visual_inputs(deployment: dict, supplied: dict) -> dict:
    if not isinstance(supplied, dict):
        raise ValueError("Flow inputs must be an object")
    inputs = {**(deployment.get("parameters") or {}), **supplied}
    schema = deployment.get("input_schema") or {}
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError("Deployment input_schema must contain properties and required")
    missing = [name for name in required if name not in inputs]
    if missing:
        raise ValueError("Missing required flow inputs: " + ", ".join(map(str, missing)))
    if schema.get("additionalProperties") is False:
        unknown = set(inputs) - set(properties)
        if unknown:
            raise ValueError("Unknown flow inputs: " + ", ".join(sorted(unknown)))
    validators = {
        "string": lambda value: isinstance(value, str),
        "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
        "boolean": lambda value: isinstance(value, bool),
        "object": lambda value: isinstance(value, dict),
        "array": lambda value: isinstance(value, list),
    }
    for name, definition in properties.items():
        if name in inputs and definition.get("type") in validators and not validators[definition["type"]](inputs[name]):
            raise ValueError(f"Flow input {name!r} must be {definition['type']}")
    return inputs


def _authorize_service_flow_call(principal, deployment: dict, short_name: str) -> None:
    if principal.source != "oauth":
        raise HTTPException(401, "Flow endpoints require an OAuth service account")
    service_account_id = principal.user_id.removeprefix("service:")
    if service_account_id not in deployment.get("allowed_service_account_ids", []):
        raise HTTPException(403, "Service account is not authorized for this flow endpoint")
    if not (principal.has_role("flow:invoke") or principal.has_role(f"flow:invoke:{short_name}")):
        raise HTTPException(403, "Service account lacks a flow invocation scope")


@app.get("/api/flow/notification-templates")
async def list_flow_notification_templates(request: Request):
    _require_flow_designer(request)
    store = _visual_store()
    try:
        return {"templates": store.list_notification_templates()}
    finally:
        store.close()


@app.post("/api/flow/notification-templates")
async def create_flow_notification_template(request: Request,
                                            body: FlowNotificationTemplate):
    principal = _require_flow_designer(request)
    if body.recipient_kind not in {"user", "group", "role"}:
        raise HTTPException(400, "recipient_kind must be user, group, or role")
    if body.channel not in {"dashboard", "internal", "chat", "email"}:
        raise HTTPException(400, "channel must be dashboard, internal, chat, or email")
    if not body.recipient_ref.strip():
        raise HTTPException(400, "recipient_ref is required")
    store = _visual_store()
    try:
        return store.create_notification_template(
            body.name.strip(), body.channel, body.recipient_kind,
            body.recipient_ref.strip(), principal.user_id,
            agent_id=body.agent_id, subject_template=body.subject_template,
            body_template=body.body_template, config=body.config,
        )
    finally:
        store.close()


@app.put("/api/flow/notification-templates/{template_id}")
async def update_flow_notification_template(request: Request, template_id: str,
                                            body: FlowNotificationTemplate):
    _require_flow_designer(request)
    if body.recipient_kind not in {"user", "group", "role"}:
        raise HTTPException(400, "recipient_kind must be user, group, or role")
    if body.channel not in {"dashboard", "internal", "chat", "email"}:
        raise HTTPException(400, "channel must be dashboard, internal, chat, or email")
    if not body.recipient_ref.strip():
        raise HTTPException(400, "recipient_ref is required")
    store = _visual_store()
    try:
        return store.update_notification_template(
            template_id, name=body.name.strip(), channel=body.channel,
            recipient_kind=body.recipient_kind, recipient_ref=body.recipient_ref.strip(),
            agent_id=body.agent_id, subject_template=body.subject_template,
            body_template=body.body_template, config=body.config,
        )
    except KeyError as exc:
        raise HTTPException(404, "Notification template not found") from exc
    finally:
        store.close()


@app.delete("/api/flow/notification-templates/{template_id}")
async def delete_flow_notification_template(request: Request, template_id: str):
    _require_flow_designer(request)
    store = _visual_store()
    try:
        if not store.delete_notification_template(template_id):
            raise HTTPException(404, "Notification template not found")
        return {"status": "deleted"}
    finally:
        store.close()


@app.get("/api/flow/inbox")
async def get_flow_notification_inbox(request: Request, limit: int = 100):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        notifications = store.list_recipient_notifications(
            principal.user_id, tuple(principal.roles), limit=max(1, min(limit, 500)))
        from gyrfalcon.flow.visual_executor import node_at_path
        for item in notifications:
            run = store.get_run(item["flow_run_id"])
            version = store.get_version(run["definition_id"], run["version"]) if run else None
            node = node_at_path(version["graph"], item["node_path"]) if version else None
            timeout = (node or {}).get("notification", {}).get("timeoutTransient")
            item["transients"] = [value for value in (node or {}).get("transients", []) if value != timeout]
        return {"notifications": notifications}
    finally:
        store.close()


@app.get("/api/flow/visual-runs")
async def list_visual_runs(request: Request, limit: int = 100, offset: int = 0,
                           state: Optional[str] = None, q: Optional[str] = None):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        rows, total = store.list_run_page(limit=limit, offset=offset, state=state,
            user_id=None if principal.is_operator else principal.user_id, search=q)
        for row in rows:
            inputs = row.get("parameters") if isinstance(row.get("parameters"), dict) else {}
            row["inventory_org"] = (inputs.get("inventory_org") or
                inputs.get("inventory_organization") or inputs.get("inventoryOrganization"))
            row["business_unit"] = (inputs.get("business_unit") or inputs.get("businessUnit")
                                    or row.get("business_unit"))
        return {"runs": rows, "total": total}
    finally:
        store.close()


@app.get("/api/flow/visual-events")
async def list_visual_events(request: Request, limit: int = 100, offset: int = 0,
                             event_type: Optional[str] = None):
    _verify_token(request)
    store = _visual_store()
    try:
        rows, total = store.list_recent_events(limit=limit, offset=offset,
                                               event_type=event_type)
        return {"events": rows, "total": total}
    finally:
        store.close()


@app.get("/api/flow/visual-deployments")
async def list_visual_deployments(request: Request):
    _require_flow_designer(request)
    store = _visual_store()
    try:
        return {"deployments": store.list_deployments()}
    finally:
        store.close()


@app.post("/api/flow/visual-deployments")
async def create_visual_deployment(request: Request, body: VisualDeploymentCreate):
    principal = _require_flow_designer(request)
    store = _visual_store()
    try:
        definition = store.get_definition(body.definition_id)
        if definition is None:
            raise HTTPException(404, "Flow definition not found")
        if not definition.get("published"):
            raise HTTPException(400, "Publish the flow before creating a deployment")
        version = body.version or (store.list_versions(body.definition_id)[0]["version"]
                                  if store.list_versions(body.definition_id) else None)
        if version is None or store.get_version(body.definition_id, version) is None:
            raise HTTPException(400, "Publish the flow version before deployment")
        return store.create_deployment(
            name=body.name.strip(), short_name=body.short_name.strip().lower(),
            definition_id=body.definition_id, version=version, user_id=principal.user_id,
            schedule=body.schedule, input_schema=body.input_schema,
            parameters=body.parameters,
            allowed_service_account_ids=body.allowed_service_account_ids,
            paused=body.paused, next_run_at=_schedule_next_at(body.schedule),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.put("/api/flow/visual-deployments/{deployment_id}")
async def update_visual_deployment(request: Request, deployment_id: str,
                                   body: VisualDeploymentCreate):
    principal = _require_flow_designer(request)
    store = _visual_store()
    try:
        current = store.get_deployment(deployment_id)
        if current is None:
            raise HTTPException(404, "Deployment not found")
        definition = store.get_definition(body.definition_id)
        if definition is None:
            raise HTTPException(404, "Flow definition not found")
        if not definition.get("published"):
            raise HTTPException(400, "Publish the flow before updating a deployment")
        version = body.version
        if version is None:
            versions = store.list_versions(body.definition_id)
            version = versions[0]["version"] if versions else None
        if version is None or store.get_version(body.definition_id, version) is None:
            raise HTTPException(400, "Publish the flow version before deployment")
        return store.update_deployment(
            deployment_id, name=body.name.strip(), short_name=body.short_name.strip().lower(),
            definition_id=body.definition_id, version=version, schedule=body.schedule,
            input_schema=body.input_schema, parameters=body.parameters,
            allowed_service_account_ids=body.allowed_service_account_ids,
            paused=int(body.paused), next_run_at=_schedule_next_at(body.schedule),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        store.close()


@app.delete("/api/flow/visual-deployments/{deployment_id}")
async def delete_visual_deployment(request: Request, deployment_id: str):
    _require_flow_designer(request)
    store = _visual_store()
    try:
        if not store.delete_deployment(deployment_id):
            raise HTTPException(404, "Deployment not found")
        return {"deleted": True}
    finally:
        store.close()


@app.post("/api/flow/visual-deployments/{deployment_id}/run")
async def run_visual_deployment(request: Request, deployment_id: str,
                                body: VisualRunRequest = VisualRunRequest()):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        deployment = store.get_deployment(deployment_id)
        if deployment is None:
            raise HTTPException(404, "Deployment not found")
        if principal.source == "oauth":
            _authorize_service_flow_call(principal, deployment, deployment["short_name"])
        try:
            inputs = _merge_visual_inputs(deployment, body.inputs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        run = store.create_run(
            deployment["definition_id"], deployment["version"], inputs, "manual",
            user_id=principal.user_id, deployment_id=deployment["id"],
            caller_key=body.caller_key or request.headers.get("Idempotency-Key"),
            trigger_ref=deployment["id"],
        )
        store.record_event(run["id"], "flow.queued", {"trigger": "manual"})
        return {"run_id": run["id"], "state": run["state"]}
    finally:
        store.close()


async def _invoke_visual_endpoint(request: Request, short_name: str,
                                  body: VisualRunRequest):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        deployment = store.get_deployment(short_name=short_name)
        if deployment is None or deployment.get("paused"):
            raise HTTPException(404, "Flow endpoint not found or paused")
        _authorize_service_flow_call(principal, deployment, short_name)
        try:
            merged = _merge_visual_inputs(deployment, body.inputs)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        run = store.create_run(
            deployment["definition_id"], deployment["version"], merged, "webhook",
            user_id=principal.user_id, deployment_id=deployment["id"],
            caller_key=body.caller_key or request.headers.get("Idempotency-Key"),
            trigger_ref=short_name,
        )
        store.record_event(run["id"], "flow.queued", {"trigger": "webhook"})
        return {"run_id": run["id"], "state": run["state"]}
    finally:
        store.close()


@app.post("/api/flow/endpoints/{short_name}", include_in_schema=False)
async def invoke_visual_endpoint(request: Request, short_name: str,
                                 body: VisualRunRequest = VisualRunRequest()):
    """Compatibility path for invoking a published flow deployment."""
    return await _invoke_visual_endpoint(request, short_name, body)


@app.post("/v1/flow/{flow_name}/", name="invoke_flow_gateway")
@app.post("/v1/flow/{flow_name}", include_in_schema=False)
async def invoke_flow_gateway(request: Request, flow_name: str,
                              body: VisualRunRequest = VisualRunRequest()):
    """Invoke a published flow through the shared `/v1` gateway listener.

    Authentication, deployment allow-list checks, input validation and
    durable run queueing are deliberately shared with the legacy flow endpoint.
    The HTTP request returns a run ID; workers execute it asynchronously.
    """
    return await _invoke_visual_endpoint(request, flow_name, body)


@app.get("/api/flow/visual-runs/{run_id}")
async def get_visual_run(request: Request, run_id: str):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Flow run not found")
        if not principal.is_operator and run["user_id"] != principal.user_id:
            raise HTTPException(403, "Flow run is not visible to this user")
        version = store.get_version(run["definition_id"], run["version"])
        return {**run, "graph": version["graph"] if version else None,
                "nodes": store.list_node_visits(run_id),
                "events": store.list_events(run_id)}
    finally:
        store.close()


class FlowRunBulkDelete(BaseModel):
    run_ids: list[str]


def _require_run_access(principal, run):
    if not principal.is_operator and run["user_id"] != principal.user_id:
        raise HTTPException(403, "Flow run is not accessible to this user")


@app.post("/api/flow/visual-runs/{run_id}/retry")
async def retry_visual_run(request: Request, run_id: str):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Flow run not found")
        _require_run_access(principal, run)
        try:
            queued = store.retry_run(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if queued is None:
            raise HTTPException(409, "Only failed or crashed runs can be retried")
        store.record_event(run_id, "flow.retry_queued", {
            "current_node_path": queued.get("current_node_path")})
        return {"run_id": run_id, "state": queued["state"],
                "current_node_path": queued.get("current_node_path")}
    finally:
        store.close()


@app.post("/api/flow/visual-runs/{run_id}/rewind")
async def rewind_visual_run(request: Request, run_id: str):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Flow run not found")
        _require_run_access(principal, run)
        if run["state"] not in {"completed", "failed", "cancelled", "crashed"}:
            raise HTTPException(409, "Only a finished run can be rewound")
        if not run.get("deployment_id"):
            raise HTTPException(409, "This run has no deployment to rewind")
        definition = store.get_definition(run["definition_id"])
        if definition is None or not definition.get("enabled", True) or not definition.get("published", False):
            raise HTTPException(409, "The flow must be enabled and published to rewind")
        try:
            replay = store.create_run(run["definition_id"], run["version"],
                run.get("parameters") or {}, "rewind", user_id=run["user_id"],
                deployment_id=run["deployment_id"], trigger_ref=run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        store.record_event(replay["id"], "flow.rewound", {"source_run_id": run_id})
        return {"run_id": replay["id"], "state": replay["state"], "source_run_id": run_id}
    finally:
        store.close()


@app.delete("/api/flow/visual-runs/{run_id}")
async def delete_visual_run(request: Request, run_id: str):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Flow run not found")
        _require_run_access(principal, run)
        result = store.delete_run(run_id)
        if result is False:
            raise HTTPException(409, "Active runs cannot be deleted")
        return {"deleted": True}
    finally:
        store.close()


@app.post("/api/flow/visual-runs/delete")
async def delete_visual_runs(request: Request, body: FlowRunBulkDelete):
    principal = _verify_token(request)
    store = _visual_store()
    deleted, active, missing = [], [], []
    try:
        for run_id in body.run_ids:
            run = store.get_run(run_id)
            if run is None:
                missing.append(run_id)
                continue
            _require_run_access(principal, run)
            result = store.delete_run(run_id)
            if result is False:
                active.append(run_id)
            elif result:
                deleted.append(run_id)
            else:
                missing.append(run_id)
        return {"deleted": deleted, "active": active, "missing": missing}
    finally:
        store.close()


@app.post("/api/flow/visual-runs/{run_id}/respond")
async def respond_to_visual_run(request: Request, run_id: str,
                                body: FlowResponseRequest):
    principal = _verify_token(request)
    store = _visual_store()
    try:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Flow run not found")
        visit = store.get_waiting_visit(run_id)
        if visit is None:
            raise HTTPException(409, "Flow run is not waiting for a response")
        version = store.get_version(run["definition_id"], run["version"])
        from gyrfalcon.flow.visual_executor import node_at_path
        node = node_at_path(version["graph"], visit["node_path"],
            reference_resolver=lambda definition_id, version_number:
                store.get_version(definition_id, version_number)["graph"])
        if visit["node_kind"] == "notification":
            notif = node.get("notification") or {}
            template_id = notif.get("templateId") or notif.get("template_id")
            template = (version.get("notification_snapshot") or {}).get(template_id, {})
            kind, recipient = template.get("recipient_kind"), template.get("recipient_ref")
            authorized = ((kind == "user" and recipient == principal.user_id)
                or (kind == "group" and (recipient in principal.roles or f"group:{recipient}" in principal.roles))
                or (kind == "role" and principal.has_role(recipient or "")))
            if not authorized:
                raise HTTPException(403, "You are not an authorized Notification recipient")
            timeout_transient = notif.get("timeoutTransient") or notif.get("timeout_transient")
            response_transients = [item for item in node.get("transients", [])
                                   if item != timeout_transient]
            if not body.transient or body.transient not in response_transients:
                raise HTTPException(400, "Choose a declared Notification transient")
            accepted = store.accept_response(visit["id"], body.value,
                                             principal.user_id, body.transient)
        else:
            agent = node.get("agent") or {}
            kind, recipient = agent.get("humanRecipientKind"), agent.get("humanRecipientRef")
            if not kind or not recipient:
                raise HTTPException(409, "Agent Activity has no configured human responder")
            authorized = ((kind == "user" and recipient == principal.user_id)
                or (kind == "group" and (recipient in principal.roles or f"group:{recipient}" in principal.roles))
                or (kind == "role" and principal.has_role(recipient)))
            if not authorized:
                raise HTTPException(403, "You are not an authorized Activity responder")
            accepted = store.accept_agent_reply(visit["id"], body.value, principal.user_id)
        if accepted is None:
            raise HTTPException(409, "Another responder won or the wait expired")
        if accepted.get("ai_session_id"):
            from gyrfalcon.db.scope import Scope
            from gyrfalcon.sessions.store import SessionStore
            session_db = SessionStore()
            try:
                session_db.append_flow_message(
                    accepted["ai_session_id"], accepted["id"],
                    content=json.dumps(body.value, ensure_ascii=False, default=str),
                    message_kind="human_reply", channel="chat", direction="inbound",
                    sender=principal.user_id,
                    scope=Scope(tenant_id=run["tenant_id"], user_id=run["user_id"]),
                )
            finally:
                session_db.close()
        store.record_event(run_id, "node.response", {"responder": principal.user_id},
                           visit_id=visit["id"])
        return {"status": "accepted", "run_id": run_id}
    finally:
        store.close()


@app.post("/api/flow/daemon/bounce")
async def bounce_visual_flow_daemon(request: Request):
    principal = _verify_token(request)
    if not principal.has_role("admin", "system_admin", "operator"):
        raise HTTPException(403, "Administrator role required")
    from gyrfalcon.flow.visual_daemon import get_visual_daemon
    get_visual_daemon().bounce()
    return {"status": "bounced"}


@app.get("/api/flow/daemon/status")
async def visual_flow_daemon_status(request: Request):
    principal = _verify_token(request)
    if not principal.has_role("admin", "system_admin", "operator"):
        raise HTTPException(403, "Administrator role required")
    from gyrfalcon.flow.visual_daemon import get_visual_daemon
    daemon = get_visual_daemon()
    return {"running": daemon._thread is not None and daemon._thread.is_alive(),
            "workers": daemon.max_workers, "active_runs": list(daemon._jobs)}


# --- Authentication (spec 15-flow.md §17.11 step 8) ---
#
# Only mounted meaningfully when identity.enabled is on. With it off the
# dashboard keeps its shared-token behaviour and every request is the LOCAL
# principal, so a single-user install sees no change at all.

@app.get("/auth/status")
async def auth_status(request: Request):
    """What the login page needs before anyone has logged in — deliberately
    unauthenticated, and deliberately says nothing about who exists."""
    from gyrfalcon import identity
    from gyrfalcon.auth.oidc import oidc_config
    from gyrfalcon.auth.store import get_auth_store

    local_enabled = _ensure_local_password_auth()
    principal = _resolve_principal(request) if local_enabled else None
    credential = get_auth_store().local_credential_for_user(principal.user_id) if principal else None
    return {
        "identity_enabled": identity.identity_enabled(),
        "oidc_configured": oidc_config().enabled,
        "login_required": local_enabled or identity.identity_enabled(),
        "authenticated": principal is not None,
        "username": "admin" if principal and credential else None,
        "must_change_password": bool(credential and credential["must_change"]),
    }


class LocalLoginRequest(BaseModel):
    username: str
    password: str


class LocalPasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/auth/local/login")
async def local_password_login(request: Request, body: LocalLoginRequest):
    from gyrfalcon.auth.session import COOKIE_NAME, get_session_store
    from gyrfalcon.auth.store import get_auth_store

    if not _ensure_local_password_auth():
        raise HTTPException(503, "Local password login is not configured")
    source_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _local_login_failures_lock:
        count, started = _local_login_failures.get(source_ip, (0, now))
        if now - started > 900:
            count, started = 0, now
        if count >= 8:
            raise HTTPException(429, "Too many sign-in attempts. Try again in 15 minutes.")
    principal, _must_change = get_auth_store().authenticate_local(body.username, body.password)
    if principal is None:
        with _local_login_failures_lock:
            _local_login_failures[source_ip] = (count + 1, started)
        raise HTTPException(401, "Invalid username or password")
    with _local_login_failures_lock:
        _local_login_failures.pop(source_ip, None)
    session = get_session_store().create(principal.user_id, principal.tenant_id)
    response = JSONResponse({"status": "signed in"})
    response.set_cookie(COOKIE_NAME, session.session_id, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https",
                        max_age=int(session.ttl), path="/")
    return response


@app.post("/auth/local/password")
async def local_password_change(request: Request, body: LocalPasswordChangeRequest):
    principal = _verify_token(request)
    from gyrfalcon.auth.store import get_auth_store
    if not get_auth_store().change_local_password(
        principal.user_id, body.current_password, body.new_password,
    ):
        raise HTTPException(400, "Current password is incorrect or this account has no local password")
    return {"status": "password changed"}


@app.get("/auth/login")
async def auth_login(request: Request, next: str = "/"):
    from gyrfalcon.auth.oidc import OIDCError, begin_login
    from gyrfalcon.auth.session import get_session_store

    try:
        url, attempt = begin_login(next_url=next)
    except OIDCError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    get_session_store().remember_attempt(attempt)
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "",
                        error: str = "", error_description: str = ""):
    from gyrfalcon.auth.oidc import OIDCError, exchange_code, identity_from_claims
    from gyrfalcon.auth.session import COOKIE_NAME, get_session_store

    if error:
        raise HTTPException(status_code=400,
                            detail=f"Sign-in failed: {error_description or error}")

    store = get_session_store()
    # Single-use: a replayable `state` would defeat the CSRF protection it is
    # there to provide.
    attempt = store.take_attempt(state)
    if attempt is None or attempt.expired():
        raise HTTPException(status_code=400,
                            detail="Login attempt is unknown or has expired; start again")
    try:
        claims = exchange_code(code, attempt)
        principal = identity_from_claims(claims)
    except OIDCError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e

    if principal is None:
        # Authenticated by the IdP, but not a member of anything here.
        # Signing in is not the same as being allowed in.
        raise HTTPException(
            status_code=403,
            detail="Your account is not a member of any organization on this server.",
        )

    session = store.create(principal.user_id, principal.tenant_id)
    response = RedirectResponse(attempt.next_url or "/", status_code=302)
    response.set_cookie(
        COOKIE_NAME, session.session_id,
        httponly=True,                       # unreadable from JavaScript
        samesite="lax",                      # survives the IdP redirect back
        secure=request.url.scheme == "https",
        max_age=int(session.ttl),
        path="/",
    )
    return response


@app.post("/auth/logout")
async def auth_logout(request: Request):
    from gyrfalcon.auth.session import COOKIE_NAME, get_session_store

    get_session_store().destroy(request.cookies.get(COOKIE_NAME))
    response = JSONResponse({"status": "signed out"})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/auth/me")
async def auth_me(request: Request):
    principal = _verify_token(request)
    orgs = []
    try:
        from gyrfalcon.auth.store import get_auth_store

        store = get_auth_store()
        for m in store.memberships(principal.user_id):
            org = store.get_org(m["org_id"])
            if org:
                orgs.append({"id": org["id"], "name": org["name"],
                             "roles": m["roles"]})
    except Exception:
        logger.debug("no identity store available", exc_info=True)
    return {
        "user_id": principal.user_id,
        "tenant_id": principal.tenant_id,
        "display_name": principal.display_name,
        "email": principal.email,
        "roles": sorted(principal.roles),
        "source": principal.source,
        "organizations": orgs,
    }


@app.get("/api/auth/keys")
async def list_api_keys(request: Request):
    principal = _verify_token(request)
    from gyrfalcon.auth.store import get_auth_store

    return {"keys": get_auth_store().list_api_keys(principal.user_id)}


def _require_security_admin(request: Request):
    principal = _verify_token(request)
    if not principal.has_role("admin"):
        raise HTTPException(403, "Administrator role required")
    return principal


class SecurityUserCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    email: str = ""
    roles: list[str] = []
    group_ids: list[str] = []


class SecurityUserUpdate(BaseModel):
    display_name: str = ""
    email: str = ""
    disabled: bool = False
    roles: list[str] = []
    group_ids: list[str] = []


class SecurityGroupBody(BaseModel):
    name: str
    description: str = ""


class SecurityRoleBody(BaseModel):
    name: str
    label: str = ""
    menu_id: Optional[str] = None
    description: str = ""
    enabled: bool = True


def _access_role_payload(role):
    return {"id": role.id, "name": role.name, "label": role.label,
            "menu_id": role.menu_id, "description": role.description,
            "enabled": role.enabled}


@app.get("/api/security/access")
async def get_security_access(request: Request):
    principal = _require_security_admin(request)
    from gyrfalcon.auth.store import get_auth_store
    from gyrfalcon.nav.store import get_nav_store
    auth = get_auth_store()
    nav = get_nav_store()
    if not nav.list_roles():
        from gyrfalcon.nav.seed import ensure_seeded
        ensure_seeded(nav)
        nav = get_nav_store()
    return {
        "users": auth.list_tenant_users(principal.tenant_id),
        "groups": auth.list_groups(principal.tenant_id),
        "roles": [_access_role_payload(role) for role in nav.list_roles()],
        "menus": [{"id": menu.id, "name": menu.name} for menu in nav.list_menus()],
    }


@app.post("/api/security/access/users")
async def create_security_user(request: Request, body: SecurityUserCreate):
    principal = _require_security_admin(request)
    from gyrfalcon.auth.store import get_auth_store
    from gyrfalcon.nav.store import get_nav_store
    allowed = {role.name for role in get_nav_store().list_roles()}
    if not set(body.roles).issubset(allowed):
        raise HTTPException(400, "One or more selected roles do not exist")
    auth = get_auth_store()
    if not set(body.group_ids).issubset({group["id"] for group in auth.list_groups(principal.tenant_id)}):
        raise HTTPException(400, "One or more selected groups do not exist")
    try:
        user = auth.create_local_user(
            principal.tenant_id, body.username, body.password, body.display_name,
            body.email, body.roles or ["app_developer"],
        )
        auth.set_user_groups(principal.tenant_id, user["id"], body.group_ids)
        user = next(u for u in auth.list_tenant_users(principal.tenant_id) if u["id"] == user["id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"user": user}


@app.put("/api/security/access/users/{user_id}")
async def update_security_user(request: Request, user_id: str, body: SecurityUserUpdate):
    principal = _require_security_admin(request)
    from gyrfalcon.auth.store import get_auth_store
    from gyrfalcon.nav.store import get_nav_store
    auth = get_auth_store()
    allowed = {role.name for role in get_nav_store().list_roles()} | {"admin"}
    if not set(body.roles).issubset(allowed):
        raise HTTPException(400, "One or more selected roles do not exist")
    if not set(body.group_ids).issubset({group["id"] for group in auth.list_groups(principal.tenant_id)}):
        raise HTTPException(400, "One or more selected groups do not exist")
    before = next((u for u in auth.list_tenant_users(principal.tenant_id) if u["id"] == user_id), None)
    if before is None:
        raise HTTPException(404, "User not found")
    if "admin" in before["roles"] and ("admin" not in body.roles or body.disabled):
        admins = [u for u in auth.list_tenant_users(principal.tenant_id) if "admin" in u["roles"] and not u["disabled"]]
        if len(admins) <= 1:
            raise HTTPException(400, "The last active administrator cannot be disabled or demoted")
    if not auth.update_tenant_user(
        principal.tenant_id, user_id, display_name=body.display_name,
        email=body.email, disabled=body.disabled, roles=body.roles,
    ):
        raise HTTPException(404, "User not found")
    try:
        if not auth.set_user_groups(principal.tenant_id, user_id, body.group_ids):
            raise HTTPException(404, "User not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user": next(u for u in auth.list_tenant_users(principal.tenant_id) if u["id"] == user_id)}


class SecurityPasswordReset(BaseModel):
    password: str


@app.post("/api/security/access/users/{user_id}/password")
async def reset_security_user_password(request: Request, user_id: str, body: SecurityPasswordReset):
    principal = _require_security_admin(request)
    from gyrfalcon.auth.store import get_auth_store
    if not get_auth_store().reset_local_password(principal.tenant_id, user_id, body.password):
        raise HTTPException(404, "Local password account not found")
    return {"status": "password reset", "must_change_password": True}


@app.post("/api/security/access/groups")
async def create_security_group(request: Request, body: SecurityGroupBody):
    principal = _require_security_admin(request)
    if not body.name.strip():
        raise HTTPException(400, "Group name is required")
    from gyrfalcon.auth.store import get_auth_store
    try:
        return {"group": get_auth_store().create_group(principal.tenant_id, body.name, body.description)}
    except Exception as exc:
        raise HTTPException(409, "A group with that name already exists") from exc


@app.put("/api/security/access/groups/{group_id}")
async def update_security_group(request: Request, group_id: str, body: SecurityGroupBody):
    principal = _require_security_admin(request)
    if not body.name.strip():
        raise HTTPException(400, "Group name is required")
    from gyrfalcon.auth.store import get_auth_store
    try:
        updated = get_auth_store().update_group(principal.tenant_id, group_id, body.name, body.description)
    except Exception as exc:
        raise HTTPException(409, "A group with that name already exists") from exc
    if not updated:
        raise HTTPException(404, "Group not found")
    return {"status": "updated"}


@app.delete("/api/security/access/groups/{group_id}")
async def delete_security_group(request: Request, group_id: str):
    principal = _require_security_admin(request)
    from gyrfalcon.auth.store import get_auth_store
    if not get_auth_store().delete_group(principal.tenant_id, group_id):
        raise HTTPException(404, "Group not found")
    return {"status": "deleted"}


@app.post("/api/security/access/roles")
async def create_security_role(request: Request, body: SecurityRoleBody):
    _require_security_admin(request)
    from gyrfalcon.nav.store import get_nav_store
    nav = get_nav_store()
    if not body.name.strip():
        raise HTTPException(400, "Role name is required")
    if body.menu_id and nav.get_menu(body.menu_id) is None:
        raise HTTPException(400, "Selected menu does not exist")
    try:
        role = nav.create_role(
            body.name.strip(), label=body.label.strip() or body.name.strip(),
            menu_id=body.menu_id or None, description=body.description.strip(), enabled=body.enabled,
        )
    except Exception as exc:
        raise HTTPException(409, "A role with that name already exists or its menu is invalid") from exc
    return {"role": _access_role_payload(role)}


@app.put("/api/security/access/roles/{role_id}")
async def update_security_role(request: Request, role_id: str, body: SecurityRoleBody):
    _require_security_admin(request)
    from gyrfalcon.nav.store import get_nav_store
    nav = get_nav_store()
    if body.menu_id and nav.get_menu(body.menu_id) is None:
        raise HTTPException(400, "Selected menu does not exist")
    try:
        role = nav.update_role(
            role_id, name=body.name.strip(), label=body.label.strip() or body.name.strip(),
            menu_id=body.menu_id or None, description=body.description.strip(), enabled=body.enabled,
        )
    except Exception as exc:
        raise HTTPException(409, "A role with that name already exists or its menu is invalid") from exc
    if role is None:
        raise HTTPException(404, "Role not found")
    return {"role": _access_role_payload(role)}


@app.delete("/api/security/access/roles/{role_id}")
async def delete_security_role(request: Request, role_id: str):
    _require_security_admin(request)
    from gyrfalcon.nav.store import get_nav_store
    if not get_nav_store().delete_role(role_id):
        raise HTTPException(404, "Role not found")
    return {"status": "deleted"}


@app.post("/api/auth/keys")
async def create_api_key(request: Request):
    """Mint a key for the caller. The plaintext is in this response and
    nowhere else — only its hash is stored."""
    principal = _verify_token(request)
    from gyrfalcon.auth.store import get_auth_store

    body = await request.json()
    key, meta = get_auth_store().mint_api_key(
        principal.user_id, principal.tenant_id, name=str(body.get("name", ""))[:100]
    )
    return {"key": key, **meta,
            "warning": "This is the only time the key is shown."}


@app.delete("/api/auth/keys/{key_id}")
async def revoke_api_key(request: Request, key_id: str):
    principal = _verify_token(request)
    from gyrfalcon.auth.store import get_auth_store

    store = get_auth_store()
    mine = {k["id"] for k in store.list_api_keys(principal.user_id)}
    if key_id not in mine:
        # 404 rather than 403: whether someone else's key exists is not the
        # caller's business.
        raise HTTPException(status_code=404, detail="No such key")
    store.revoke_api_key(key_id)
    return {"status": "revoked", "id": key_id}


# --- SPA Serving ---

def _get_spa_dir() -> Path:
    logger.debug("Beginning of _get_spa_dir")
    return Path(__file__).parent.parent / "web" / "dist"


@app.get("/")
async def serve_spa_root():
    spa_dir = _get_spa_dir()
    index = spa_dir / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Dashboard not built</h1><p>Run: cd web && npm run build</p>")
    html = index.read_text()
    html = html.replace("<title>AI Foundry Dashboard</title>", f"<title>{get_app_name()} Dashboard</title>")
    # Inject session token
    html = html.replace(
        "</head>",
        f'<script>window.__GYRFALCON_SESSION_TOKEN__="{_session_token}";'
        f'window.__GYRFALCON_BASE_PATH__="";'
        f'window.__GYRFALCON_APP_NAME__={json.dumps(get_app_name())};'
        f'window.__GYRFALCON_ORG_NAME__={json.dumps(get_org_name())};</script></head>'
    )
    # Prevent caching to ensure fresh token on server restart
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


# Catch-all: serve SPA index.html for any non-API route (client-side routing)
@app.get("/{path:path}")
async def serve_spa_fallback(path: str):
    # Don't intercept API routes
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    spa_dir = _get_spa_dir()
    # Serve static assets directly
    asset_file = spa_dir / path
    if asset_file.exists() and asset_file.is_file():
        from fastapi.responses import FileResponse
        return FileResponse(str(asset_file))
    # Otherwise serve index.html for SPA routing
    index = spa_dir / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>Dashboard not built</h1><p>Run: cd web && npm run build</p>")
    html = index.read_text()
    html = html.replace("<title>AI Foundry Dashboard</title>", f"<title>{get_app_name()} Dashboard</title>")
    html = html.replace(
        "</head>",
        f'<script>window.__GYRFALCON_SESSION_TOKEN__="{_session_token}";'
        f'window.__GYRFALCON_BASE_PATH__="";'
        f'window.__GYRFALCON_APP_NAME__={json.dumps(get_app_name())};'
        f'window.__GYRFALCON_ORG_NAME__={json.dumps(get_org_name())};</script></head>'
    )
    # Prevent caching to ensure fresh token on server restart
    return HTMLResponse(html, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })


def run_dashboard():
    """Launch the web dashboard."""
    logger.debug("Beginning of run_dashboard")
    import uvicorn
    import threading as _t

    # Validate PostgreSQL and apply the Alembic baseline before starting any
    # background workers. Otherwise the flow runner can fail repeatedly in a
    # daemon thread while the dashboard appears to have started successfully.
    from sqlalchemy.exc import SQLAlchemyError

    from gyrfalcon.db import open_database
    from gyrfalcon.db.migrations import ensure_schema

    database = None
    try:
        database = open_database()
        ensure_schema(database)
    except (ValueError, RuntimeError, SQLAlchemyError) as exc:
        raise SystemExit(
            "Cannot start the dashboard without a reachable PostgreSQL database. "
            "Set flow.store.dsn in config.yaml or GYRFALCON_DB_DSN. "
            f"Details: {exc}"
        ) from exc
    finally:
        if database is not None:
            database.close()


    # Load plugins at startup, matching the CLI (cli.py) and the TUI gateway
    # (tui_gateway/server.py). Plugins register new agent capabilities (tools,
    # hooks, CLI commands); without this the dashboard could serve for minutes
    # with none of them wired up, unlike the other two entry points.
    from gyrfalcon.plugins import PluginManager
    PluginManager().discover_and_load()

    # Separately, import any user flow files (~/.gyrfalcon/flows/, no manifest
    # needed — see gyrfalcon/flow/registry.py). This is not plugin loading: a
    # flow is a complete definition on its own the moment it's decorated, so it
    # gets its own directory rather than riding on the plugin mechanism.
    from gyrfalcon.flow.registry import discover_flows
    discover_flows()

    # The visual daemon owns durable runs and deployment schedules.
    from gyrfalcon.flow.visual_daemon import get_visual_daemon
    visual_daemon = get_visual_daemon()
    visual_daemon.start()

    config = load_config()
    host = config.get("web", {}).get("host", "127.0.0.1")
    port = config.get("web", {}).get("port", 9119)

    # Pre-warm Copilot token so first chat message pays no token-exchange latency
    def _prewarm():
        try:
            provider = cfg_get("provider.active", "copilot")
            if provider == "copilot":
                from gyrfalcon.providers.copilot import get_copilot_token
                get_copilot_token()
                logger.debug("Copilot token pre-warmed")
        except Exception:
            pass
    _t.Thread(target=_prewarm, daemon=True, name="token-prewarm").start()

    print(f"\n  {get_app_name()} Dashboard: http://{host}:{port}")
    print(f"  Session Token: {_session_token}\n")

    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        visual_daemon.stop()
        from gyrfalcon.db import dispose_database_pools
        dispose_database_pools()
