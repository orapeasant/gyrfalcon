"""JSON-RPC server — dispatches requests, manages agent lifecycle, routes events."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional, TYPE_CHECKING

from gyrfalcon.gyrfalcon_logging import get_logger, set_session_tag
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.run_agent import AIAgent
from gyrfalcon.config import load_config, cfg_get
from gyrfalcon.plugins import PluginManager
from gyrfalcon.tools.mcp_tool import initialize_mcp_servers
from gyrfalcon.telemetry import WebSocketTracer, AgentTracer, trace_span
from gyrfalcon.gyrfalcon_constants import get_app_name

if TYPE_CHECKING:
    from tui_gateway.transport import BaseTransport

logger = get_logger("tui_gateway.server")


class TUIGatewayServer:
    """JSON-RPC dispatcher, agent spawn, event routing."""

    def __init__(self, transport: "BaseTransport", ws_tracer: Optional[WebSocketTracer] = None):
        self.transport = transport
        self._session_db = SessionDB()
        self._agent: Optional[AIAgent] = None
        self._session_id: Optional[str] = None
        self._busy = False
        self._busy_lock = threading.Lock()
        self._plugin_manager = PluginManager()
        self._plugin_manager.discover_and_load()
        self._ws_tracer = ws_tracer or WebSocketTracer()
        self._agent_tracer: Optional[AgentTracer] = None
        initialize_mcp_servers()

        # Register handlers
        self._handlers: dict[str, Callable] = {
            "prompt.submit": self._handle_prompt_submit,
            "session.create": self._handle_session_create,
            "session.list": self._handle_session_list,
            "session.resume": self._handle_session_resume,
            "slash.exec": self._handle_slash_exec,
            "complete.slash": self._handle_complete_slash,
            "complete.path": self._handle_complete_path,
            "approval.respond": self._handle_approval_respond,
            "clarify.respond": self._handle_clarify_respond,
            "config.get": self._handle_config_get,
            "agent.interrupt": self._handle_interrupt,
            "model.get_state": self._handle_model_get_state,
            "model.list_models": self._handle_model_list_models,
            "model.set": self._handle_model_set,
        }

    def start(self) -> None:
        """Start processing incoming messages."""
        # Send ready event
        logger.debug("Beginning of start")
        skin_data = self._load_skin()
        self._emit_event("gateway.ready", {"skin": skin_data, "version": "0.1.0"})

        # Process loop
        self.transport.on_message(self._handle_message)
        self.transport.start()

    def stop(self) -> None:
        """Graceful shutdown."""
        logger.debug("Beginning of stop")
        from gyrfalcon.tools.mcp_tool import shutdown_mcp_servers
        shutdown_mcp_servers()
        self._session_db.close()
        self.transport.stop()

    def _handle_message(self, raw: str) -> None:
        """Process incoming JSON-RPC message."""
        logger.debug("Beginning of _handle_message")
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        method = msg.get("method")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        handler = self._handlers.get(method)
        if handler:
            try:
                result = handler(params)
                if msg_id is not None:
                    self._send_response(msg_id, result)
            except Exception as e:
                logger.error(f"Handler error for {method}: {e}", exc_info=True)
                if msg_id is not None:
                    self._send_error(msg_id, str(e))
        else:
            if msg_id is not None:
                self._send_error(msg_id, f"Unknown method: {method}")

    def _send_response(self, msg_id: Any, result: Any) -> None:
        logger.debug("Beginning of _send_response")
        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        self.transport.send(json.dumps(response))

    def _send_error(self, msg_id: Any, error: str) -> None:
        logger.debug("Beginning of _send_error")
        response = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -1, "message": error}}
        self.transport.send(json.dumps(response))

    def _emit_event(self, event_type: str, data: dict | None = None) -> None:
        """Emit a notification event to the frontend with tracing."""
        notification = {
            "jsonrpc": "2.0",
            "method": event_type,
            "params": data or {},
        }
        serialized = json.dumps(notification)

        # Hot-path streaming events never get individual OTel spans (too frequent).
        # Flush behaviour on these is controlled by performance.stream_flush_enabled
        # (default False = no per-chunk syscall).
        _HOT = ("message.delta", "thinking.block", "tool.start", "tool.complete")
        if event_type in _HOT:
            flush = cfg_get("performance.stream_flush_enabled", False)
            self.transport.send(serialized, flush=flush)
            return

        # All other events: honour telemetry toggle, always flush.
        if cfg_get("performance.telemetry_enabled", False):
            span = self._ws_tracer.trace_message_sent(event_type, data or {})
            try:
                self.transport.send(serialized, flush=True)
            finally:
                span.end()
        else:
            self.transport.send(serialized, flush=True)

    def _handle_prompt_submit(self, params: dict) -> dict:
        """Handle user message submission."""
        logger.debug("Beginning of _handle_prompt_submit")
        message = params.get("message", "")
        if not message:
            return {"error": "Empty message"}

        with self._busy_lock:
            if self._busy:
                return {"error": "Agent is busy"}
            self._busy = True

        # Start turn span for tracing
        self._ws_tracer.start_turn_span(message)

        # Run agent in background thread
        thread = threading.Thread(
            target=self._run_agent_turn, args=(message,), daemon=True
        )
        thread.start()

        return {"status": "processing", "session_id": self._session_id}

    def _run_agent_turn(self, message: str) -> None:
        """Execute agent turn with streaming events and tracing.

        Slash-command pre-processing
        ────────────────────────────
        If the message starts with a known invocation prefix we resolve it
        *before* spawning the agent so the correct system-prompt and toolsets
        are in place for the full agent loop:

          /skill <name>       → load SKILL.md body as system-prompt override;
                                the remaining text (if any) becomes the user message.
          /mcp <server>       → restrict enabled toolsets to that MCP server.
          /tool <tool_name>   → pass the tool name as context hint.
          /toolset <name>     → restrict enabled toolsets to that toolset.
        """
        logger.debug("Beginning of _run_agent_turn")
        response_length = 0
        error_msg = None

        # ── Slash-command resolution ──────────────────────────────────────────
        system_override: str | None = None
        effective_message = message
        enabled_toolsets: list[str] | None = None

        stripped = message.strip()

        # /allow-all — grant session-level allow-all and confirm
        if stripped.lower() in ("/allow-all", "/allowall"):
            from gyrfalcon.tools.approval import set_allow_all
            set_allow_all(self._session_id or "", True)
            self._emit_event("message.complete", {
                "content": (
                    "✅ **Allow-All mode activated** for this session.\n\n"
                    "All tool calls and shell commands will be executed **without approval prompts** "
                    "(except unconditionally blocked commands like filesystem wipes).\n\n"
                    "To disable: `/allow-all off`"
                ),
                "session_id": self._session_id,
            })
            with self._busy_lock:
                self._busy = False
            self._emit_event("status.update", {"state": "idle"})
            return

        if stripped.lower() in ("/allow-all off", "/allowall off", "/allow-all disable"):
            from gyrfalcon.tools.approval import set_allow_all
            set_allow_all(self._session_id or "", False)
            self._emit_event("message.complete", {
                "content": "🔒 **Allow-All mode disabled.** Approval prompts restored.",
                "session_id": self._session_id,
            })
            with self._busy_lock:
                self._busy = False
            self._emit_event("status.update", {"state": "idle"})
            return

        if stripped.startswith("/skill ") or stripped.lower().startswith("/skill "):
            # /skill <name> [optional extra instructions]
            parts = stripped.split(None, 2)          # ["/skill", "<name>", "rest"]
            skill_name = parts[1] if len(parts) > 1 else ""
            extra      = parts[2] if len(parts) > 2 else ""
            if skill_name:
                try:
                    from gyrfalcon.tools.skills_tool import skill_view
                    import json as _json
                    raw = skill_view({"name": skill_name})
                    skill_data = _json.loads(raw)
                    if "error" not in skill_data:
                        skill_body = skill_data.get("body", "").strip()
                        skill_desc = skill_data.get("description", "")
                        if skill_body:
                            system_override = skill_body
                            # User message becomes either the extra text or a
                            # generic "execute this skill" instruction so the
                            # agent loop has something to act on.
                            effective_message = extra.strip() if extra.strip() else (
                                f"Execute the skill '{skill_name}'. "
                                f"Follow the skill instructions exactly and complete the task fully."
                            )
                            self._emit_event("status.update", {
                                "state": "thinking",
                                "hint": f"⚡ Running skill: {skill_name}",
                            })
                            logger.info(f"Skill '{skill_name}' loaded as system prompt ({len(skill_body)} chars)")
                        else:
                            # Skill exists but has no body — warn and fall through
                            self._emit_event("message.complete", {
                                "content": f"⚠️ Skill `{skill_name}` was found but has no instructions body in SKILL.md.",
                                "session_id": self._session_id,
                            })
                            with self._busy_lock:
                                self._busy = False
                            self._emit_event("status.update", {"state": "idle"})
                            return
                    else:
                        self._emit_event("message.complete", {
                            "content": f"⚠️ Skill not found: `{skill_name}`\n\n{skill_data['error']}",
                            "session_id": self._session_id,
                        })
                        with self._busy_lock:
                            self._busy = False
                        self._emit_event("status.update", {"state": "idle"})
                        return
                except Exception as e:
                    logger.error(f"Failed to load skill '{skill_name}': {e}")
                    # Fall through — let the agent try to handle it

        elif stripped.startswith("/toolset "):
            parts = stripped.split(None, 2)
            ts_name = parts[1] if len(parts) > 1 else ""
            effective_message = parts[2] if len(parts) > 2 else f"Use the {ts_name} toolset to complete the task."
            if ts_name:
                enabled_toolsets = [ts_name]

        elif stripped.startswith("/mcp "):
            parts = stripped.split(None, 2)
            srv_name = parts[1] if len(parts) > 1 else ""
            effective_message = parts[2] if len(parts) > 2 else f"Use {srv_name} to complete the task."
            if srv_name:
                enabled_toolsets = ["mcp"]

        elif stripped.startswith("/tool "):
            # Just pass through as a hint in the message — tool names map to
            # registered tool handlers already visible to the agent.
            parts = stripped.split(None, 2)
            tool_name = parts[1] if len(parts) > 1 else ""
            rest = parts[2] if len(parts) > 2 else ""
            effective_message = f"Use the tool `{tool_name}` to complete this task. {rest}".strip()

        # ── Agent execution ───────────────────────────────────────────────────
        try:
            agent = self._get_or_create_agent()

            # Apply skill system-prompt override for this turn only:
            # store the old override, inject the skill body, restore after.
            _prev_override = agent.system_prompt_override
            _prev_system   = agent._system_message
            _prev_toolsets = agent.enabled_toolsets

            if system_override:
                agent.system_prompt_override = system_override
                agent._system_message = None          # force rebuild with new override

            if enabled_toolsets is not None:
                agent.enabled_toolsets = enabled_toolsets

            # Set session tag for this thread's log context
            if self._session_id:
                set_session_tag(f"[{self._session_id[:8]}] ")
                self._ws_tracer.session_id = self._session_id

            self._emit_event("status.update", {"state": "thinking"})

            # Trace the agent conversation turn
            with trace_span("agent.conversation_turn", {
                "agent.session_id": self._session_id or "unknown",
                "agent.model": agent.model,
                "agent.message_length": len(effective_message),
                "agent.skill_override": bool(system_override),
            }) as span:
                result = agent.run_conversation(user_message=effective_message)

                response = result.get("final_response", "")
                response_length = len(response)
                span.set_attribute("agent.response_length", response_length)

                if "usage" in result:
                    span.set_attribute("llm.usage.input_tokens", result["usage"].get("input_tokens", 0))
                    span.set_attribute("llm.usage.output_tokens", result["usage"].get("output_tokens", 0))

            # Restore previous agent state
            agent.system_prompt_override = _prev_override
            agent._system_message        = _prev_system
            agent.enabled_toolsets       = _prev_toolsets

            self._emit_event("message.complete", {
                "content": response,
                "session_id": self._session_id,
            })

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Agent turn failed: {e}", exc_info=True)
            self._emit_event("error", {"message": error_msg})
        finally:
            with self._busy_lock:
                self._busy = False
            self._emit_event("status.update", {"state": "idle"})

    def _get_or_create_agent(self) -> AIAgent:
        """Get existing agent or create new one."""
        logger.debug("Beginning of _get_or_create_agent")
        if self._agent:
            return self._agent

        config = load_config()
        model = config.get("model", {}).get("name", "")
        provider_name = cfg_get("provider.active", "copilot")

        base_url, api_key, provider = self._resolve_credentials(provider_name)

        if not model:
            from gyrfalcon.providers import get_provider_profile
            prof = get_provider_profile(provider_name)
            model = (prof.default_model if prof and prof.default_model else None) or "gpt-4o"

        self._agent = AIAgent(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider=provider,
            session_id=self._session_id,
            session_db=self._session_db,
            stream_delta_callback=self._on_stream_delta,
            thinking_delta_callback=self._on_thinking_block,
            tool_progress_callback=self._on_tool_progress,
            plugin_manager=self._plugin_manager,
        )
        self._session_id = self._agent.session_id
        return self._agent

    def _resolve_credentials(self, provider_name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Resolve (base_url, api_key, provider_tag) for a given provider name."""
        import os
        from gyrfalcon.providers import get_provider_profile

        if provider_name == "copilot":
            from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated
            if is_authenticated():
                base_url, api_key = get_copilot_credentials()
                return base_url, api_key, "copilot"
            return None, None, None

        profile = get_provider_profile(provider_name)
        if not profile:
            logger.warning(f"Unknown provider: {provider_name}, falling back to copilot")
            return self._resolve_credentials("copilot")

        # API-key based providers: read key from env
        if profile.auth_type == "api_key" and profile.env_vars:
            api_key = next((os.environ.get(ev) for ev in profile.env_vars if os.environ.get(ev)), None)
            return profile.base_url, api_key, profile.name

        # Bedrock: no api_key needed (uses AWS SDK)
        if profile.auth_type == "aws_sdk":
            return profile.base_url, "dummy", profile.name

        # Anthropic
        if provider_name == "anthropic":
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            return profile.base_url, api_key, "anthropic"

        return profile.base_url, None, profile.name

    def _on_stream_delta(self, delta: str) -> None:
        logger.debug("Beginning of _on_stream_delta")
        self._emit_event("message.delta", {"content": delta})

    def _on_thinking_block(self, text: str, elapsed: float) -> None:
        """Emit thinking block event if show_reasoning is enabled."""
        logger.debug("Beginning of _on_thinking_block")
        if not cfg_get("display.show_reasoning", True):
            return
        self._emit_event("thinking.block", {
            "content": text,
            "elapsed": round(elapsed, 1),
        })

    def _on_tool_progress(self, tool_name: str, args: dict, status: str) -> None:
        logger.debug("Beginning of _on_tool_progress")
        from gyrfalcon.tools import registry as _reg
        from gyrfalcon.tools.approval import is_allow_all, needs_approval_for_tool
        if status == "start":
            # Determine if tool is read-only or write
            is_ro = _reg.is_read_only(tool_name)
            allow_all = is_allow_all(self._session_id)
            # For terminal commands, check the command arg
            command = args.get("command") if tool_name == "terminal" else None
            _, reason = needs_approval_for_tool(tool_name, command, self._session_id)
            self._emit_event("tool.start", {
                "name": tool_name,
                "args": args,
                "read_only": is_ro,
                "allow_all": allow_all,
                "write": not is_ro,
            })
        elif status == "complete":
            self._emit_event("tool.complete", {"name": tool_name})

    def _handle_session_create(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_session_create")
        self._agent = None
        self._session_id = None
        return {"status": "created"}

    def _handle_session_list(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_session_list")
        limit = params.get("limit", 20)
        sessions = self._session_db.list_sessions(limit=limit)
        return {"sessions": sessions}

    def _handle_session_resume(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_session_resume")
        session_id = params.get("session_id", "")
        session = self._session_db.get_session(session_id)
        if not session:
            return {"error": f"Session not found: {session_id}"}
        self._session_id = session_id
        self._agent = None
        messages = self._session_db.get_messages_as_conversation(session_id)
        return {"session": session, "messages": messages}

    def _handle_slash_exec(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_slash_exec")
        command = params.get("command", "")
        # Basic slash command handling
        parts = command.strip().split(None, 1)
        cmd = parts[0].lstrip("/") if parts else ""
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "new":
            self._agent = None
            self._session_id = None
            return {"output": "New session started"}
        elif cmd == "model":
            if arg:
                if self._agent:
                    self._agent.model = arg
                return {"output": f"Model set to: {arg}"}
            return {"output": f"Current model: {self._agent.model if self._agent else 'not set'}"}
        elif cmd == "sessions":
            sessions = self._session_db.list_sessions(limit=10)
            lines = [f"{s['id'][:8]} | {s.get('title') or 'untitled'}" for s in sessions]
            return {"output": "\n".join(lines)}

        return {"error": f"Unknown command: /{cmd}"}

    def _handle_complete_slash(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_complete_slash")
        prefix = params.get("prefix", "")
        commands = ["new", "model", "sessions", "resume", "tools", "skills", "help", "quit"]
        matches = [c for c in commands if c.startswith(prefix)]
        return {"completions": matches}

    def _handle_complete_path(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_complete_path")
        import glob as globmod
        prefix = params.get("prefix", "")
        matches = globmod.glob(prefix + "*")[:20]
        return {"completions": matches}

    def _handle_approval_respond(self, params: dict) -> dict:
        # TODO: Wire to agent approval system
        logger.debug("Beginning of _handle_approval_respond")
        return {"status": "acknowledged"}

    def _handle_clarify_respond(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_clarify_respond")
        return {"status": "acknowledged"}

    def _handle_config_get(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_config_get")
        key = params.get("key", "")
        if key:
            return {"value": cfg_get(key)}
        return {"config": load_config()}

    def _handle_interrupt(self, params: dict) -> dict:
        logger.debug("Beginning of _handle_interrupt")
        if self._agent:
            self._agent.interrupt()
        return {"status": "interrupted"}

    def _handle_model_get_state(self, params: dict) -> dict:
        """Return current provider, model, and all available providers with auth status."""
        logger.debug("Beginning of _handle_model_get_state")
        import os
        from gyrfalcon.providers import list_providers, get_provider_profile

        current_provider = cfg_get("provider.active", "copilot")
        current_model = cfg_get("model.name", "") or ""

        # Prefer live agent model if active
        if self._agent and self._agent.model:
            current_model = self._agent.model

        if not current_model:
            current_model = "gpt-4o"

        providers_out = []
        for pname in list_providers():
            profile = get_provider_profile(pname)
            if not profile:
                continue
            authenticated = False
            if profile.auth_type == "copilot":
                from gyrfalcon.providers.copilot import is_authenticated
                authenticated = is_authenticated()
            elif profile.auth_type == "api_key":
                authenticated = any(bool(os.environ.get(ev)) for ev in profile.env_vars)
            elif profile.auth_type == "aws_sdk":
                authenticated = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
            providers_out.append({
                "name": profile.name,
                "display_name": profile.display_name or profile.name.title(),
                "authenticated": authenticated,
            })

        return {
            "provider": current_provider,
            "model": current_model,
            "providers": providers_out,
        }

    def _handle_model_list_models(self, params: dict) -> dict:
        """List available models for a given provider."""
        logger.debug("Beginning of _handle_model_list_models")
        provider_name = params.get("provider", "copilot")
        from gyrfalcon.providers import get_provider_profile

        profile = get_provider_profile(provider_name)
        if not profile:
            return {"models": [], "error": f"Unknown provider: {provider_name}"}
        try:
            models = profile.fetch_models()
        except Exception as e:
            logger.warning(f"fetch_models failed for {provider_name}: {e}")
            models = profile.fallback_models or []
        return {"models": models}

    def _handle_model_set(self, params: dict) -> dict:
        """Persist provider + model selection and reset the agent."""
        logger.debug("Beginning of _handle_model_set")
        provider = params.get("provider", "")
        model = params.get("model", "")
        if not provider or not model:
            return {"error": "provider and model are required"}

        cfg_set("provider.active", provider)
        cfg_set("model.name", model)

        # Reset agent so next prompt picks up the new provider/model
        self._agent = None

        self._emit_event("model.changed", {"provider": provider, "model": model})
        logger.info(f"Model set to {provider}/{model}")
        return {"status": "ok", "provider": provider, "model": model}

    def _load_skin(self) -> dict:
        """Load active skin configuration."""
        logger.debug("Beginning of _load_skin")
        skin_name = cfg_get("display.skin", "default")
        return {
            "name": skin_name,
            "agent_name": get_app_name(),
            "prompt_symbol": "❯",
        }
