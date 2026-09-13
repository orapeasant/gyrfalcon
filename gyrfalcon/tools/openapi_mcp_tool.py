"""OpenAPI MCP execution — turns an `openapi`-type `mcp.json` entry into real,
callable REST tools.

Spec: docs/spec/gyrfalcon/13-odata-mcp.md, "Server Types" table, says OpenAPI
servers are "configuration-only at present; their tools are resolved at query
time." Nothing previously did that resolution: `initialize_mcp_servers()`
(`mcp_tool.py`) only ever connected `stdio` servers, and no other module built
tool schemas or made calls from an OpenAPI spec. This module is that missing
step — it plugs into the same places a `stdio` server already does, so the
rest of the pipeline (toolset resolution, agent attachment) needs no special
case for it:

  - one tool per *enabled* entity (`config["entities"][operationId].enabled`),
    named `mcp_{server_name}_{operationId}` — same naming as
    `MCPConnection._discover_tools` uses for stdio tools;
  - registered under toolset `f"mcp-{server_name}"` — same toolset name a
    stdio server's tools use, so `mcp_servers` on an Agent (see
    `gyrfalcon/agents.py`) and `enabled_toolsets` on a session both already
    know how to name it.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import urljoin

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.tools import registry

logger = get_logger("tools.openapi_mcp")

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def fetch_spec(url: str) -> dict:
    """Fetch and parse an OpenAPI document (JSON or YAML), direct-first with
    proxy fallback — the same `net.httpx_request` path everything else in the
    codebase uses, so this respects the same 2s probe / 5-minute failure cache
    as every other outbound call."""
    from gyrfalcon.net import httpx_request

    resp = httpx_request(
        "GET", url,
        headers={"Accept": "application/json, application/yaml, text/yaml"},
        timeout=30.0, follow_redirects=True,
    )
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "yaml" in content_type or url.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError("YAML OpenAPI spec requires PyYAML: pip install pyyaml") from e
        spec = yaml.safe_load(resp.text)
    else:
        spec = json.loads(resp.text)

    if not isinstance(spec, dict):
        raise ValueError("Invalid OpenAPI spec: root must be an object")
    return spec


def _resolve_ref(spec: dict, ref: str) -> dict:
    """`"#/components/schemas/Pet"` -> `spec["components"]["schemas"]["Pet"]`.
    Only local (`#/...`) refs — an external-file ref is left as `{}` rather
    than fetching arbitrary URLs on the LLM's behalf."""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def _deref(spec: dict, schema: Any) -> Any:
    if isinstance(schema, dict) and "$ref" in schema:
        return _resolve_ref(spec, schema["$ref"])
    return schema


def _base_url(spec_url: str, spec: dict) -> str:
    """`servers[0].url` is commonly relative (e.g. `/api/v31`) — resolve it
    against the spec document's own origin, matching how a browser would
    resolve it, not the literal string."""
    servers = spec.get("servers") or []
    server_url = (servers[0].get("url") if servers and isinstance(servers[0], dict) else None) or "/"
    return urljoin(spec_url, server_url)


def _build_tool_schema(spec: dict, method: str, path: str, op: dict, path_item: dict) -> tuple[dict, dict]:
    """Returns (json_schema, meta). `meta` records which properties are path
    vs. query vs. header params and whether there's a body, so the handler
    can route resolved arguments back to the right part of the HTTP request
    without re-parsing the spec on every call."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    meta: dict[str, Any] = {"path": [], "query": [], "header": [], "has_body": False}

    params = list(path_item.get("parameters") or []) + list(op.get("parameters") or [])
    for p in params:
        p = _deref(spec, p)
        name, loc = p.get("name"), p.get("in")
        if not name or loc not in ("path", "query", "header"):
            continue
        schema = dict(_deref(spec, p.get("schema")) or {"type": "string"})
        schema["description"] = p.get("description", schema.get("description", ""))
        properties[name] = schema
        meta[loc].append(name)
        if p.get("required") or loc == "path":
            required.append(name)

    body = op.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content", {})
        json_content = content.get("application/json") or next(iter(content.values()), {})
        body_schema = _deref(spec, json_content.get("schema")) or {"type": "object"}
        properties["body"] = body_schema
        meta["has_body"] = True
        if body.get("required"):
            required.append("body")

    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema, meta


def _apply_auth(headers: dict, auth_cfg: dict) -> Optional[tuple]:
    """Mutates `headers` for header-based auth; returns an httpx `auth` tuple
    for basic auth, or None. OAuth (client-credentials token fetch) is not
    implemented here — a server configured for it registers no tools rather
    than silently calling out unauthenticated."""
    auth_type = (auth_cfg or {}).get("type", "none")
    sub = (auth_cfg or {}).get(auth_type, {}) or {}
    if auth_type == "basic" and sub.get("username"):
        return (sub.get("username", ""), sub.get("password", ""))
    if auth_type == "apikey" and sub.get("api_key"):
        headers[sub.get("api_key_header") or "Authorization"] = sub["api_key"]
    return None


def _make_handler(base_url: str, method: str, path_template: str, meta: dict,
                   auth_cfg: dict, server_name: str, op_id: str):
    def handler(args: dict, **_kwargs) -> str:
        from gyrfalcon.net import httpx_request

        args = args or {}
        url_path = path_template
        for name in meta["path"]:
            if name in args:
                url_path = url_path.replace("{" + name + "}", str(args[name]))

        query = {k: v for k, v in args.items() if k in meta["query"] and v is not None}
        headers: dict[str, str] = {k: str(args[k]) for k in meta["header"] if k in args}
        basic_auth = _apply_auth(headers, auth_cfg)
        json_body = args.get("body") if meta.get("has_body") else None

        full_url = base_url.rstrip("/") + "/" + url_path.lstrip("/")
        try:
            resp = httpx_request(
                method, full_url, headers=headers or None, params=query or None,
                json=json_body, auth=basic_auth, timeout=30.0,
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the LLM, not raised
            return json.dumps({"error": f"{server_name}.{op_id} request failed: {e}"})

        try:
            body_out: Any = resp.json()
        except ValueError:
            body_out = resp.text[:5000]
        return json.dumps({"status": resp.status_code, "body": body_out})

    return handler


def register_openapi_server(server_name: str, config: dict) -> int:
    """Fetch `config["url"]`, build one tool per enabled entity, register
    them under toolset `mcp-{server_name}`. Returns the count registered.

    Re-entrant: calling this again for the same server (e.g. a dashboard
    save) just overwrites the previous registrations — `registry.register`
    keys on tool name, there is nothing to unregister first.
    """
    if not config.get("enabled", True):
        return 0

    spec_url = config.get("url")
    if not spec_url:
        logger.warning(f"OpenAPI server {server_name!r} has no url; skipping")
        return 0

    try:
        spec = fetch_spec(spec_url)
    except Exception as e:
        logger.warning(f"OpenAPI server {server_name!r}: could not fetch/parse spec: {e}")
        return 0

    base_url = _base_url(spec_url, spec)
    auth_cfg = config.get("auth") or {"type": "none"}
    entities = config.get("entities") or {}

    registered = 0
    paths = spec.get("paths") or {}
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, op in path_item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(op, dict):
                continue
            op_id = op.get("operationId") or f"{method.upper()}_{path.strip('/').replace('/', '_')}"
            entity_cfg = entities.get(op_id)
            if not entity_cfg or not entity_cfg.get("enabled"):
                continue

            schema, meta = _build_tool_schema(spec, method, path, op, path_item)
            tool_name = f"mcp_{server_name}_{op_id}"
            description = (
                entity_cfg.get("description") or op.get("summary") or op.get("description")
                or f"{method.upper()} {path}"
            )
            registry.register(
                name=tool_name,
                toolset=f"mcp-{server_name}",
                schema={"name": tool_name, "description": description, "parameters": schema},
                handler=_make_handler(base_url, method.upper(), path, meta, auth_cfg, server_name, op_id),
            )
            registered += 1

    logger.info(f"OpenAPI server {server_name!r}: registered {registered} tool(s) from {spec_url}")
    return registered
