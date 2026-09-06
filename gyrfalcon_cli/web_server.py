"""Web Dashboard — FastAPI backend serving REST API, WebSocket PTY bridge, and React SPA."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
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

app = FastAPI(title=f"{get_app_name()} Dashboard API")

# Instrument FastAPI with OpenTelemetry
if _telemetry_enabled:
    instrument_fastapi(app)


def _verify_token(request: Request) -> None:
    """Verify session token from header."""
    logger.debug("Beginning of _verify_token")
    token = request.headers.get("X-Gyrfalcon-Session-Token")
    if token != _session_token:
        raise HTTPException(status_code=401, detail="Invalid session token")


# --- Status ---

# Track engine state - the dashboard server itself hosts the engine
_engine_initialized = False

# --- Agent event bus ---
# Maps session_id → list of WebSocket send coroutines subscribed to that session.
# Background agent threads push events here; the WS handler forwards them.
import asyncio as _asyncio
import threading as _ebus_lock_mod

_agent_event_subscribers: dict[str, list] = {}   # session_id → [asyncio.Queue]
_agent_event_lock = _ebus_lock_mod.Lock()


def _agent_event_subscribe(session_id: str, queue: "_asyncio.Queue") -> None:
    with _agent_event_lock:
        _agent_event_subscribers.setdefault(session_id, []).append(queue)


def _agent_event_unsubscribe(session_id: str, queue: "_asyncio.Queue") -> None:
    with _agent_event_lock:
        subs = _agent_event_subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)
        if not subs:
            _agent_event_subscribers.pop(session_id, None)


def _agent_event_publish(session_id: str, event: dict) -> None:
    """Called from background thread — pushes event to all subscriber queues."""
    with _agent_event_lock:
        queues = list(_agent_event_subscribers.get(session_id, []))
    for q in queues:
        try:
            q.put_nowait(event)
        except Exception:
            pass


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
        cursor = db.conn.execute("SELECT COUNT(*) FROM sessions")
        total = cursor.fetchone()[0]
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


# --- Agents ---

def _load_agents() -> list[dict]:
    from gyrfalcon.gyrfalcon_constants import get_agents_file
    p = get_agents_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("agents", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


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
    return {"agents": [_agent_entry(a) for a in _load_agents()]}


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
    from gyrfalcon.config import cfg_get

    sid = session_id or str(_uuid.uuid4())

    # ── Pre-create session so session.resume returns immediately ─────────────
    # We just create the session row — AIAgent will write the real messages.
    pre_db = SessionDB()
    try:
        if not pre_db.get_session(sid):
            model = agent_cfg.get("model") or cfg_get("model.name", "")
            pre_db.create_session(session_id=sid, source="chat", model=model)
    finally:
        pre_db.close()

    # ── Build skill-injected instructions ────────────────────────────────────
    instr = agent_cfg.get("instructions", "").strip()
    skills = agent_cfg.get("skills", [])
    if skills:
        from gyrfalcon.tools.skills_tool import skill_view as _sv
        import json as _j
        parts = [instr] if instr else []
        for sk in skills:
            try:
                d = _j.loads(_sv({"name": sk}))
                if d.get("body"):
                    parts.append(f"\n\n## Skill: {sk}\n{d['body']}")
            except Exception:
                pass
        full_instructions = "\n".join(parts)
    else:
        full_instructions = instr

    # ── Background runner ────────────────────────────────────────────────────
    def _run() -> None:
        _agent_event_publish(sid, {"method": "status.update", "params": {"state": "thinking"}})
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

            # Resolve provider credentials (base_url, api_key)
            _provider_name = agent_cfg.get("provider") or cfg_get("provider.active", "copilot")
            _base_url, _api_key = None, None
            if _provider_name == "copilot":
                from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated
                if is_authenticated():
                    _base_url, _api_key = get_copilot_credentials()
            else:
                from gyrfalcon.providers import get_provider_profile as _gpp
                _prof = _gpp(_provider_name)
                if _prof:
                    _base_url = _prof.base_url
                    if _prof.auth_type == "api_key" and _prof.env_vars:
                        _api_key = next((os.environ.get(ev) for ev in _prof.env_vars if os.environ.get(ev)), None)

            agent = AIAgent(
                base_url=_base_url,
                api_key=_api_key,
                model=agent_cfg.get("model") or cfg_get("model.name", ""),
                provider=_provider_name,
                max_iterations=agent_cfg.get("max_iterations", 30),
                quiet_mode=False,
                skip_memory=False,
                platform="chat",
                session_id=sid,
                enabled_toolsets=agent_cfg.get("enabled_toolsets") or None,
                system_prompt_override=full_instructions or None,
                stream_delta_callback=_on_delta,
                tool_progress_callback=_on_tool,
            )
            # Don't re-send the user message — it was already persisted above.
            # We call run_conversation directly, skipping the DB seed.
            result = agent.run_conversation(user_message=message)
            response = result.get("final_response", "")
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
    _loop = _asyncio_ws.get_event_loop()

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
        """Subscribe this WS to agent events for session_id if not already."""
        if session_id in _active_agent_queues:
            return
        with _agent_event_lock:
            if session_id not in _agent_event_subscribers:
                return   # No agent running for this session
        q: _asyncio_ws.Queue = _asyncio_ws.Queue()
        _active_agent_queues[session_id] = q
        _agent_event_subscribe(session_id, q)
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

    uvicorn.run(app, host=host, port=port, log_level="warning")
