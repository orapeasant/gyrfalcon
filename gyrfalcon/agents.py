"""Shared helpers for user-defined Agents (dashboard "Agents" feature).

An Agent is a saved config (instructions, skills, toolsets, model, provider,
max_iterations) stored as JSON via `get_agents_file()`. Invoking one creates a
session tagged with its `agent_id`; every subsequent turn on that session —
whether typed via the invoke API or continued from the chat page — must
rebuild the `AIAgent` from the same config, or the agent's persona and tools
silently disappear after the first reply. Both `gyrfalcon_cli/web_server.py`
(invoke) and `tui_gateway/server.py` (chat continuation) call into this
module so the two paths can't drift apart again.
"""

import json
from typing import Any, Optional


def load_agents() -> list[dict]:
    from gyrfalcon.gyrfalcon_constants import get_agents_file

    p = get_agents_file()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get("agents", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def get_agent(agent_id: str) -> Optional[dict]:
    return next((a for a in load_agents() if a["id"] == agent_id), None)


def build_full_instructions(agent_cfg: dict) -> str:
    """Merge an agent's system instructions with the body of its attached skills."""
    from gyrfalcon.tools.skills_tool import skill_view

    instr = (agent_cfg.get("instructions") or "").strip()
    skills = agent_cfg.get("skills", [])
    if not skills:
        return instr

    parts = [instr] if instr else []
    for sk in skills:
        try:
            d = json.loads(skill_view({"name": sk}))
            if d.get("body"):
                parts.append(f"\n\n## Skill: {sk}\n{d['body']}")
        except Exception:
            pass
    return "\n".join(parts)


def resolve_provider_credentials(provider_name: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve (base_url, api_key) for a provider name, as used to invoke an Agent."""
    import os

    if provider_name == "copilot":
        from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated

        if is_authenticated():
            return get_copilot_credentials()
        return None, None

    from gyrfalcon.providers import get_provider_profile

    prof = get_provider_profile(provider_name)
    if not prof:
        return None, None
    base_url = prof.base_url
    api_key = None
    if prof.auth_type == "api_key" and prof.env_vars:
        api_key = next((os.environ.get(ev) for ev in prof.env_vars if os.environ.get(ev)), None)
    return base_url, api_key


def build_agent_kwargs(agent_cfg: dict) -> dict[str, Any]:
    """AIAgent constructor kwargs derived from a saved Agent config.

    Callers still need to supply session-specific kwargs (session_id,
    session_db, callbacks, platform, etc).
    """
    from gyrfalcon.config import cfg_get

    provider_name = agent_cfg.get("provider") or cfg_get("provider.active", "copilot")
    base_url, api_key = resolve_provider_credentials(provider_name)

    # An attached MCP server ("MCP Servers" in the Agent editor) is additive,
    # not a replacement for the agent's own toolset selection — the dashboard
    # presents them as two separate fields, and an agent that picks one MCP
    # server would otherwise lose every core tool the moment it does. Each
    # server's tools live under toolset f"mcp-{name}" (mcp_tool.py /
    # openapi_mcp_tool.py both register there); this was previously dropped
    # entirely, which is why an attached MCP server's tools never reached the
    # running agent regardless of server type.
    base_toolsets = list(agent_cfg.get("enabled_toolsets") or ["core"])
    mcp_toolsets = [f"mcp-{name}" for name in (agent_cfg.get("mcp_servers") or [])]
    enabled_toolsets = base_toolsets + mcp_toolsets

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": agent_cfg.get("model") or cfg_get("model.name", ""),
        "provider": provider_name,
        "max_iterations": agent_cfg.get("max_iterations", 30),
        "enabled_toolsets": enabled_toolsets or None,
        "system_prompt_override": build_full_instructions(agent_cfg) or None,
    }
