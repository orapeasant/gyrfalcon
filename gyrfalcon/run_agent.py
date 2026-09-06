"""AIAgent — The central agent class managing the full conversation lifecycle."""

from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

from gyrfalcon.gyrfalcon_logging import get_logger, set_session_tag
from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.config import load_config, cfg_get
from gyrfalcon.model_tools import get_tool_definitions, handle_function_call, discover_builtin_tools
from gyrfalcon.agent.prompt_builder import build_system_prompt
from gyrfalcon.agent.context_compressor import ContextCompressor
from gyrfalcon.agent.model_metadata import get_model_context_length, estimate_request_tokens_rough
from gyrfalcon.agent.memory_manager import MemoryManager
from gyrfalcon.agent.auxiliary_client import call_llm as auxiliary_call_llm
from gyrfalcon.utils import base_url_hostname, base_url_host_matches
from gyrfalcon.net import get_openai_client_with_fallback, get_anthropic_client

logger = get_logger("agent")

_TITLE_MAX_CHARS = 80
_FALLBACK_TITLE = "Untitled session"


def _clean_title(raw: str) -> str:
    """Normalize a model-produced title: strip quoting, punctuation, and newlines."""
    title = " ".join((raw or "").split())
    title = title.strip().strip('"').strip("'").rstrip(".").strip()
    return title[:_TITLE_MAX_CHARS]


def derive_fallback_title(user_message: str) -> str:
    """Build a readable title from the first user message.

    Used as the immediate placeholder so a session is never left untitled, and as
    the permanent title when the summarizing call is unavailable (no credentials,
    offline, rate-limited).
    """
    text = " ".join((user_message or "").split())
    if not text:
        return _FALLBACK_TITLE
    if len(text) <= 60:
        return text[:_TITLE_MAX_CHARS]
    # Cut at the last word boundary so the title doesn't end mid-word.
    clipped = text[:60].rsplit(" ", 1)[0] or text[:60]
    return f"{clipped}…"[:_TITLE_MAX_CHARS]


class IterationBudget:
    """Thread-safe iteration counter shared between parent and subagents."""

    def __init__(self, total: int):
        self._total = total
        self._consumed = 0
        self._lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        """Returns True if budget remains after consumption."""
        with self._lock:
            self._consumed += n
            return self._consumed < self._total

    def refund(self, n: int = 1) -> None:
        with self._lock:
            self._consumed = max(0, self._consumed - n)

    def reset(self) -> None:
        """Reset budget for a new conversation turn."""
        with self._lock:
            self._consumed = 0

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self._total - self._consumed)

    @property
    def total(self) -> int:
        return self._total


class AIAgent:
    """Central agent class managing the full conversation lifecycle."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        provider: str | None = None,
        api_mode: str | None = None,
        model: str = "",
        max_iterations: int = 90,
        max_tokens: int | None = None,
        enabled_toolsets: list[str] | None = None,
        disabled_toolsets: list[str] | None = None,
        quiet_mode: bool = False,
        save_trajectories: bool = False,
        platform: str | None = None,
        session_id: str | None = None,
        session_db: SessionDB | None = None,
        skip_context_files: bool = False,
        skip_memory: bool = False,
        credential_pool: Any | None = None,
        reasoning_config: dict | None = None,
        service_tier: str | None = None,
        fallback_model: str | None = None,
        checkpoints_enabled: bool = False,
        tool_progress_callback: Callable | None = None,
        stream_delta_callback: Callable | None = None,
        thinking_delta_callback: Callable | None = None,
        clarify_callback: Callable | None = None,
        budget: IterationBudget | None = None,
        plugin_manager: Any | None = None,
        temperature: float | None = None,
        system_prompt_override: str | None = None,
    ):
        self.base_url = base_url
        self.api_key = api_key
        self.provider = provider
        self.model = model or cfg_get("model.name", "")
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens or cfg_get("model.max_tokens")
        self.enabled_toolsets = enabled_toolsets
        self.disabled_toolsets = disabled_toolsets
        self.quiet_mode = quiet_mode
        self.save_trajectories = save_trajectories
        self.platform = platform or "cli"
        self.session_id = session_id
        self.session_db = session_db
        self.skip_context_files = skip_context_files
        self.skip_memory = skip_memory
        self.credential_pool = credential_pool
        self.reasoning_config = reasoning_config
        self.service_tier = service_tier
        self.fallback_model = fallback_model
        self.checkpoints_enabled = checkpoints_enabled
        self.tool_progress_callback = tool_progress_callback
        self.stream_delta_callback = stream_delta_callback
        self.thinking_delta_callback = thinking_delta_callback
        self.clarify_callback = clarify_callback
        self._api_call_start_time: float = 0.0
        self.plugin_manager = plugin_manager
        self.temperature = temperature or cfg_get("model.temperature")
        self.system_prompt_override = system_prompt_override

        # Resolve effective max_iterations:
        # explicit constructor arg > agent.max_llm_call config > agent.max_turns config > built-in default
        config_max_llm_call = cfg_get("agent.max_llm_call")
        config_max_turns    = cfg_get("agent.max_turns", 90)
        if max_iterations != 90:
            # Caller passed an explicit value — honour it
            self.max_iterations = max_iterations
        elif config_max_llm_call is not None:
            self.max_iterations = int(config_max_llm_call)
        else:
            self.max_iterations = int(config_max_turns)

        # Auto-detect API mode
        self.api_mode = api_mode or self._detect_api_mode()

        # Internal state
        self._budget = budget or IterationBudget(max_iterations)
        self._interrupt_requested = False
        self._interrupt_lock = threading.Lock()
        self._conversation_history: list[dict] = []
        self._system_message: str = ""
        self._client = None
        self._anthropic_client = None

        # Context compression
        context_length = get_model_context_length(
            self.model, self.base_url, self.api_key,
            config_context_length=cfg_get("model.context_length"),
            provider=self.provider,
        )
        self._compressor = ContextCompressor(
            model=self.model,
            context_length=context_length,
            threshold_percent=cfg_get("compression.threshold_percent", 0.50),
            protect_first_n=cfg_get("compression.protect_first_n", 3),
        )

        # Memory
        self._memory_manager = MemoryManager()

        # Discover tools
        discover_builtin_tools()

    def _detect_api_mode(self) -> str:
        """Auto-detect API mode from base_url hostname patterns."""
        logger.debug("Beginning of _detect_api_mode")
        if not self.base_url:
            logger.debug("End of _detect_api_mode")
            return "chat_completions"
        hostname = base_url_hostname(self.base_url)
        if "anthropic" in hostname:
            logger.debug("End of _detect_api_mode")
            return "anthropic_messages"
        if "bedrock-runtime" in hostname:
            logger.debug("End of _detect_api_mode")
            return "bedrock_converse"
        if "codex" in hostname or "responses" in (self.base_url or ""):
            logger.debug("End of _detect_api_mode")
            return "codex_responses"
        logger.debug("End of _detect_api_mode")
        return "chat_completions"

    def _get_client(self):
        """Lazy client initialization with proxy-first fallback."""
        logger.debug("Beginning of _get_client")
        if self.api_mode == "anthropic_messages":
            if self._anthropic_client is None:
                self._anthropic_client = get_anthropic_client(api_key=self.api_key)
            logger.debug("End of _get_client")
            return self._anthropic_client

        # For Copilot provider, refresh the short-lived token before each client use.
        # The token expires in ~30 min; get_copilot_token() handles caching/re-exchange.
        if self.provider == "copilot" or (
            self.base_url and "githubcopilot.com" in self.base_url
        ):
            from gyrfalcon.providers.copilot import get_copilot_token
            fresh_token = get_copilot_token()
            if fresh_token and fresh_token != self.api_key:
                logger.debug("Copilot token refreshed — recreating OpenAI client")
                self.api_key = fresh_token
                self._client = None  # force client rebuild with new token

        if self._client is None:
            self._client = get_openai_client_with_fallback(
                base_url=self.base_url,
                api_key=self.api_key or "dummy",
            )
        logger.debug("End of _get_client")
        return self._client

    def _ensure_session_title(self, user_message: str, assistant_response: str) -> None:
        """Guarantee the session has a title, then refine it with the LLM.

        A placeholder derived from the first user message is written synchronously
        so the title is never empty — the LLM call can fail, return nothing, or be
        cut short when a short-lived process exits, and previously each of those
        left the title NULL forever.
        """
        import threading

        existing = self.session_db.get_session(self.session_id)  # type: ignore[union-attr]
        if existing and existing.get("title"):
            return  # already titled — nothing to do

        fallback = derive_fallback_title(user_message)
        try:
            self.session_db.update_session_title(self.session_id, fallback)  # type: ignore[union-attr]
        except Exception as e:
            logger.warning(f"Could not write fallback session title: {e}")
            return

        def _run() -> None:
            try:
                prompt = (
                    "Generate a short, descriptive title (max 8 words) for this conversation. "
                    "Output ONLY the title text — no quotes, no punctuation at the end.\n\n"
                    f"User: {user_message[:300]}\n"
                    f"Assistant: {assistant_response[:300]}"
                )
                client = self._get_client()
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=64,
                    temperature=0.3,
                )
                title = _clean_title(resp.choices[0].message.content or "")
                if title:
                    self.session_db.update_session_title(self.session_id, title)  # type: ignore[union-attr]
                    logger.info(f"Session title set: {title!r}")
                else:
                    logger.warning("Title generation returned empty; keeping fallback title")
            except Exception as e:
                # Never fatal — the fallback title is already persisted.
                logger.warning(f"Title generation failed, keeping fallback: {e}")

        # Not a daemon: a one-shot CLI or scheduler run must not exit mid-write.
        threading.Thread(target=_run, daemon=False, name="title-gen").start()

    def chat(self, message: str) -> str:
        """Simple interface — returns final response string."""
        logger.debug("Beginning of chat")
        result = self.run_conversation(user_message=message)
        logger.debug("End of chat")
        return result.get("final_response", "")

    def run_conversation(
        self,
        user_message: str,
        system_message: str | None = None,
        conversation_history: list[dict] | None = None,
        task_id: str | None = None,
    ) -> dict:
        """Full interface — returns dict with final_response + messages."""
        logger.debug("Beginning of run_conversation")
        # Initialize session
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
            if self.session_db:
                self.session_db.create_session(
                    session_id=self.session_id,
                    source=self.platform,
                    model=self.model,
                )

        set_session_tag(f"[{self.session_id[:8]}] ")

        # Build system prompt
        if system_message:
            self._system_message = system_message
        elif self.system_prompt_override:
            self._system_message = self.system_prompt_override
        elif not self._system_message:
            memory_guidance = ""
            if not self.skip_memory:
                self._memory_manager.initialize(self.session_id)
                memory_guidance = self._memory_manager.build_system_prompt()

            from gyrfalcon.model_tools import get_enabled_tool_names
            self._system_message = build_system_prompt(
                enabled_tools=get_enabled_tool_names(self.enabled_toolsets, self.disabled_toolsets),
                enabled_toolsets=self.enabled_toolsets,
                platform_name=self.platform,
                skip_context_files=self.skip_context_files,
                skip_memory=self.skip_memory,
                memory_guidance=memory_guidance,
            )

        # Set conversation history
        if conversation_history is not None:
            self._conversation_history = conversation_history
        
        # Add user message
        self._conversation_history.append({"role": "user", "content": user_message})

        # Persist user message
        if self.session_db:
            self.session_db.append_message(self.session_id, "user", user_message)

        # Get tool definitions (None if empty to skip tool handling)
        tools = get_tool_definitions(self.enabled_toolsets, self.disabled_toolsets, self.quiet_mode)

        # Add memory tool schemas
        if not self.skip_memory:
            memory_schemas = self._memory_manager.get_tool_schemas()
            for schema in memory_schemas:
                tools.append({"type": "function", "function": schema})

        # Pass None instead of empty list to skip tool handling entirely
        tools = tools if tools else None

        # Main conversation loop
        self._budget.reset()
        result = self._run_loop(tools, task_id)

        # Title any session that still lacks one. Not gated on history length:
        # a first turn that used tools already exceeds it, which is exactly the
        # case that was silently never titled.
        if self.session_db and self.session_id and result.get("final_response"):
            self._ensure_session_title(user_message, result["final_response"])

        # Sync memory
        if not self.skip_memory and result.get("final_response"):
            self._memory_manager.sync_all(user_message, result["final_response"])

        logger.debug("End of run_conversation")
        return result

    def _run_loop(self, tools: list[dict] | None, task_id: str | None) -> dict:
        """Main conversation loop: LLM call → tool dispatch → loop or respond."""
        logger.debug("Beginning of _run_loop")
        api_call_count = 0
        grace_call = False

        while (api_call_count < self.max_iterations and self._budget.remaining > 0) or grace_call:
            # Check interrupt
            with self._interrupt_lock:
                if self._interrupt_requested:
                    logger.info("Interrupt requested, breaking loop")
                    break

            grace_call = False

            # Check context compression
            if self._compressor.should_compress(self._conversation_history, self._system_message):
                logger.info("Compressing context...")
                self._conversation_history = self._compressor.compress(
                    self._conversation_history,
                    self._system_message,
                    task_id=task_id,
                    auxiliary_client=type("AC", (), {"call_llm": staticmethod(auxiliary_call_llm)})(),
                )

            # Make API call
            try:
                response = self._api_call(tools)
                logger.info(f"calling tools")
            except Exception as e:
                logger.error(f"API call failed: {e}", exc_info=True)
                # Log conversation history on error for debugging
                logger.error(f"Conversation history at time of error: {len(self._conversation_history)} messages")
                for i, msg in enumerate(self._conversation_history):
                    logger.error(f"  msg[{i}]: role={msg.get('role')}, content={repr(msg.get('content'))[:100]}, tool_calls={bool(msg.get('tool_calls'))}")
                # Try fallback model
                if self.fallback_model and self.model != self.fallback_model:
                    logger.info(f"Trying fallback model: {self.fallback_model}")
                    original_model = self.model
                    self.model = self.fallback_model
                    try:
                        response = self._api_call(tools)
                    except Exception as e2:
                        self.model = original_model
                        return {"final_response": f"Error: {e2}", "messages": self._conversation_history}
                    self.model = original_model
                else:
                    return {"final_response": f"Error: {e}", "messages": self._conversation_history}

            logger.info("After Make API Call")

            # Process response
            if self.api_mode == "anthropic_messages":
                return self._process_anthropic_response(response, tools, task_id, api_call_count)
            else:
                result = self._process_openai_response(response, tools, task_id, api_call_count)
                if result is not None:
                    return result

            api_call_count += 1
            self._budget.consume()

        # Budget exhausted
        logger.warning(
            f"max_llm_call limit reached ({self.max_iterations} calls). "
            "Stopping agent loop. Increase agent.max_llm_call in config.yaml to allow more."
        )
        logger.debug("End of _run_loop")
        msg = (
            f"[Stopped: max_llm_call limit of {self.max_iterations} reached. "
            "The agent was cut off before completing. "
            "Increase `agent.max_llm_call` in your config to allow more calls.]"
        )
        return {
            "final_response": msg,
            "messages": self._conversation_history,
        }

    def _api_call(self, tools: list[dict] | None) -> Any:
        """Make LLM API call based on api_mode."""
        logger.debug("Beginning of _api_call")
        client = self._get_client()

        if self.api_mode == "anthropic_messages":
            result = self._anthropic_api_call(client, tools)
            logger.debug("End of _api_call")
            return result
        else:
            result = self._openai_api_call(client, tools)
            logger.debug("End of _api_call")
            return result

    def _sanitize_messages(self, messages: list[dict]) -> list[dict]:
        """Normalise conversation history before sending to any OpenAI-compatible API.

        Problems fixed:
        - assistant messages with tool_calls must not have content=None/null;
          use "" instead (Copilot API and several others reject null).
        - tool messages must have a string content, never None.
        - skip malformed messages that have no role.
        """
        out = []
        for msg in messages:
            role = msg.get("role")
            if not role:
                continue
            if role == "assistant":
                content = msg.get("content") or ""
                cleaned: dict = {"role": "assistant", "content": content}
                if msg.get("tool_calls"):
                    cleaned["tool_calls"] = msg["tool_calls"]
                out.append(cleaned)
            elif role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": msg.get("content") or "",
                })
            else:
                out.append(msg)
        return out

    def _openai_api_call(self, client, tools: list[dict] | None) -> Any:
        """OpenAI-compatible API call."""
        logger.debug("Beginning of _openai_api_call")
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self._system_message}]
                        + self._sanitize_messages(self._conversation_history),
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.service_tier:
            kwargs["service_tier"] = self.service_tier

        # Reasoning config
        if self.reasoning_config:
            effort = self.reasoning_config.get("effort")
            if effort and effort != "none":
                kwargs["reasoning_effort"] = effort

        # Provider-specific extras (e.g. Copilot headers)
        if self.provider:
            from gyrfalcon.providers import get_provider_profile
            profile = get_provider_profile(self.provider)
            if profile:
                extras = profile.build_api_kwargs_extras()
                if extras:
                    kwargs.update(extras)

        # ── Request debug log ──────────────────────────────────────────────
        if logger.isEnabledFor(10):  # DEBUG level
            self._log_request(kwargs)

        # Streaming — request usage in the final chunk via stream_options
        if self.stream_delta_callback:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}
            result = self._streaming_openai_call(client, kwargs)
            logger.debug("End of _openai_api_call")
            return result
        else:
            result = client.chat.completions.create(**kwargs)
            if logger.isEnabledFor(10):
                self._log_response(result)
            logger.debug("End of _openai_api_call")
            return result

    # ── Debug helpers ──────────────────────────────────────────────────────

    def _log_request(self, kwargs: dict) -> None:
        """Log full API request (headers + body) at DEBUG level."""
        import json as _json

        # Headers come from extra_headers kwarg (provider extras)
        headers = dict(kwargs.get("extra_headers", {}))
        # Mask the Authorization header value – it's set by the OpenAI client
        # from self.api_key; we show a preview so we can confirm the token
        api_key = self.api_key or ""
        if api_key:
            preview = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 12 else "***"
            headers["Authorization"] = f"Bearer {preview}"
        headers["Content-Type"] = "application/json"

        # Body = everything except extra_headers
        body = {k: v for k, v in kwargs.items() if k != "extra_headers"}

        # Truncate individual message content for readability
        messages_preview = []
        for msg in body.get("messages", []):
            m = dict(msg)
            if isinstance(m.get("content"), str) and len(m["content"]) > 300:
                m["content"] = m["content"][:300] + f"…[{len(msg['content'])} chars]"
            messages_preview.append(m)
        body_preview = {**body, "messages": messages_preview}

        logger.debug(
            "--- LLM REQUEST -------------------------------------------\n"
            "  base_url : %s\n"
            "  headers  : %s\n"
            "  body     :\n%s\n"
            "-----------------------------------------------------------",
            self.base_url or "(default)",
            _json.dumps(headers, indent=4),
            _json.dumps(body_preview, indent=4, default=str),
        )

    def _log_response(self, response: Any) -> None:
        """Log full API response at DEBUG level (non-streaming)."""
        import json as _json
        try:
            choice = response.choices[0] if response.choices else None
            usage = getattr(response, "usage", None)
            logger.debug(
                "--- LLM RESPONSE ------------------------------------------\n"
                "  model        : %s\n"
                "  finish_reason: %s\n"
                "  usage        : in=%s out=%s\n"
                "  content      : %s\n"
                "  tool_calls   : %s\n"
                "-----------------------------------------------------------",
                getattr(response, "model", "?"),
                getattr(choice, "finish_reason", "?") if choice else "?",
                getattr(usage, "prompt_tokens", "?") if usage else "?",
                getattr(usage, "completion_tokens", "?") if usage else "?",
                repr(getattr(choice.message, "content", None))[:400] if choice else "?",
                _json.dumps(
                    [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in (choice.message.tool_calls or [])
                    ]
                    if choice and choice.message.tool_calls else [],
                    indent=4,
                ),
            )
        except Exception as e:
            logger.debug("_log_response error: %s", e)

    def _streaming_openai_call(self, client, kwargs: dict) -> Any:
        """Handle streaming OpenAI call, collecting full response."""
        logger.debug("Beginning of _streaming_openai_call")
        stream = client.chat.completions.create(**kwargs)

        content_parts = []
        tool_calls_data: dict[int, dict] = {}
        finish_reason = None
        stream_usage = None   # captured from the final chunk if the API sends it

        for chunk in stream:
            # Capture usage from the final usage-only chunk (OpenAI sends it last)
            if hasattr(chunk, "usage") and chunk.usage:
                stream_usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason

            if delta.content:
                content_parts.append(delta.content)
                if self.stream_delta_callback:
                    self.stream_delta_callback(delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_data:
                        tool_calls_data[idx] = {
                            "id": tc.id or "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    if tc.id:
                        tool_calls_data[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls_data[idx]["function"]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls_data[idx]["function"]["arguments"] += tc.function.arguments

        # Build synthetic response object
        content = "".join(content_parts) or None
        tool_calls = list(tool_calls_data.values()) if tool_calls_data else None

        # ── Streaming response debug log ──────────────────────────────────
        if logger.isEnabledFor(10):
            import json as _json
            logger.debug(
                "--- LLM RESPONSE (stream) ---------------------------------\n"
                "  finish_reason: %s\n"
                "  content      : %s\n"
                "  tool_calls   : %s\n"
                "-----------------------------------------------------------",
                finish_reason,
                repr(content)[:400] if content else "None",
                _json.dumps(tool_calls or [], indent=4, default=str),
            )

        class Choice:
            def __init__(self, content, tool_calls, finish_reason):
                self.message = type("Msg", (), {
                    "content": content,
                    "tool_calls": [
                        type("TC", (), {
                            "id": tc["id"],
                            "type": "function",
                            "function": type("Fn", (), {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            })(),
                        })()
                        for tc in (tool_calls or [])
                    ] if tool_calls else None,
                    "role": "assistant",
                })()
                self.finish_reason = finish_reason

        class Response:
            def __init__(self, choice, usage=None):
                self.choices = [choice]
                self.usage = usage

        logger.debug("End of _streaming_openai_call")
        return Response(Choice(content, tool_calls, finish_reason), stream_usage)

    def _anthropic_api_call(self, client, tools: list[dict] | None) -> Any:
        """Anthropic native API call."""
        logger.debug("Beginning of _anthropic_api_call")
        # Convert OpenAI tool format to Anthropic format
        anthropic_tools = []
        if tools:
            for tool in tools:
                if tool.get("type") == "function":
                    fn = tool["function"]
                    anthropic_tools.append({
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                    })

        # Convert messages to Anthropic format
        messages = []
        for msg in self._conversation_history:
            if msg["role"] == "tool":
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }],
                })
            elif msg["role"] == "assistant" and msg.get("tool_calls"):
                content = []
                if msg.get("content"):
                    content.append({"type": "text", "text": msg["content"]})
                for tc in msg["tool_calls"]:
                    content.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["function"]["name"],
                        "input": json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    })
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": msg["role"], "content": msg.get("content", "")})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": self._system_message,
            "messages": messages,
            "max_tokens": self.max_tokens or 4096,
        }

        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        # Enable extended thinking when caller wants thinking output.
        # Thinking requires temperature=1 so we drop any custom temperature.
        if self.thinking_delta_callback:
            budget_tokens = cfg_get("model.thinking_budget_tokens", 8000)
            max_tok = kwargs.get("max_tokens", 4096)
            # budget must be < max_tokens; clamp if needed
            budget_tokens = min(int(budget_tokens), max(1024, int(max_tok) - 1024))
            kwargs["thinking"] = {"type": "thinking", "budget_tokens": budget_tokens}
            kwargs.pop("temperature", None)

        self._api_call_start_time = time.time()
        result = client.messages.create(**kwargs)
        logger.debug("End of _anthropic_api_call")
        return result

    def _process_openai_response(
        self, response, tools: list[dict], task_id: str | None, api_call_count: int
    ) -> Optional[dict]:
        """Process OpenAI response. Returns result dict if done, None to continue loop."""
        logger.debug("Beginning of _process_openai_response")
        choice = response.choices[0]
        message = choice.message

        # Track tokens + cost
        if response.usage and self.session_db:
            in_tok  = getattr(response.usage, "prompt_tokens", 0)
            out_tok = getattr(response.usage, "completion_tokens", 0)
            reason_tok = getattr(getattr(response.usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
            from gyrfalcon.pricing import calculate_cost, usd_to_aic
            call_cost = calculate_cost(self.model, in_tok, out_tok, reason_tok)
            self.session_db.update_token_counts(
                self.session_id, # type: ignore[arg-type]
                input_tokens=in_tok,
                output_tokens=out_tok,
                reasoning_tokens=reason_tok,
                cost=call_cost,
            )
            logger.info(
                "tokens: in=%d out=%d%s | cost=%.6f USD (%.4f AIC) | model=%s",
                in_tok, out_tok,
                f" reason={reason_tok}" if reason_tok else "",
                call_cost, usd_to_aic(call_cost), self.model,
            )

        # Tool calls
        if message.tool_calls:
            # Add assistant message with tool calls.
            # Use "" instead of None for content — the Copilot API (and many
            # OpenAI-compatible providers) reject {"content": null} with tool_calls.
            assistant_msg = {"role": "assistant", "content": message.content or ""}
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in message.tool_calls
            ]
            self._conversation_history.append(assistant_msg)

            if self.session_db:
                self.session_db.append_message(
                    self.session_id, "assistant", message.content,  # type: ignore[arg-type]
                    tool_calls=assistant_msg["tool_calls"],
                )

            # Execute each tool
            for tc in message.tool_calls:
                tool_name = tc.function.name
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}

                # Progress callback
                if self.tool_progress_callback:
                    self.tool_progress_callback(tool_name, tool_args, "start")

                # Check if it's a memory tool
                if tool_name == "memory" and not self.skip_memory:
                    result = self._memory_manager.route_tool_call(tool_name, tool_args)
                else:
                    result = handle_function_call(
                        function_name=tool_name,
                        function_args=tool_args,
                        task_id=task_id,
                        tool_call_id=tc.id,
                        session_id=self.session_id,
                        plugin_manager=self.plugin_manager,
                    )

                if self.tool_progress_callback:
                    self.tool_progress_callback(tool_name, tool_args, "complete")

                # Add tool result - content must be a string, never None
                tool_result = result if result else ""
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                }
                self._conversation_history.append(tool_msg)

                if self.session_db:
                    self.session_db.append_message(
                        self.session_id, "tool", result,# type: ignore[arg-type]
                        tool_call_id=tc.id, tool_name=tool_name,
                    )

            logger.debug("End of _process_openai_response (continue loop)")
            return None  # Continue loop

        # Text response — we're done
        final_response = message.content or ""
        self._conversation_history.append({"role": "assistant", "content": final_response})

        if self.session_db:
            self.session_db.append_message(self.session_id, "assistant", final_response) # type: ignore[arg-type]

        logger.debug("End of _process_openai_response")
        return {
            "final_response": final_response,
            "messages": self._conversation_history,
            "session_id": self.session_id,
        }

    def _process_anthropic_response(
        self, response, tools: list[dict], task_id: str | None, api_call_count: int
    ) -> dict:
        """Process Anthropic response."""
        logger.debug("Beginning of _process_anthropic_response")

        # Track tokens from Anthropic usage (field names differ from OpenAI)
        if hasattr(response, "usage") and response.usage and self.session_db:
            in_tok     = getattr(response.usage, "input_tokens", 0) or 0
            out_tok    = getattr(response.usage, "output_tokens", 0) or 0
            reason_tok = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            from gyrfalcon.pricing import calculate_cost, usd_to_aic
            call_cost = calculate_cost(self.model, in_tok, out_tok, 0)
            self.session_db.update_token_counts(
                self.session_id,  # type: ignore[arg-type]
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost=call_cost,
            )
            logger.info(
                "tokens: in=%d out=%d%s | cost=%.6f USD (%.4f AIC) | model=%s",
                in_tok, out_tok,
                f" cache_read={reason_tok}" if reason_tok else "",
                call_cost, usd_to_aic(call_cost), self.model,
            )

        # Check for tool use
        tool_uses = [block for block in response.content if block.type == "tool_use"]
        text_blocks = [block for block in response.content if block.type == "text"]
        thinking_blocks = [block for block in response.content if block.type == "thinking"]

        # Fire thinking callback with accumulated text and elapsed wall-clock time
        if thinking_blocks and self.thinking_delta_callback:
            thinking_text = "\n\n".join(b.thinking for b in thinking_blocks if hasattr(b, "thinking"))
            elapsed = time.time() - self._api_call_start_time
            self.thinking_delta_callback(thinking_text, elapsed)

        text_content = "\n".join(b.text for b in text_blocks) if text_blocks else ""

        if tool_uses:
            # Build assistant message with tool calls
            tool_calls = [{
                "id": tu.id,
                "type": "function",
                "function": {
                    "name": tu.name,
                    "arguments": json.dumps(tu.input),
                },
            } for tu in tool_uses]

            self._conversation_history.append({
                "role": "assistant",
                "content": text_content if text_content else None,
                "tool_calls": tool_calls,
            })

            # Execute tools
            for tu in tool_uses:
                if self.tool_progress_callback:
                    self.tool_progress_callback(tu.name, tu.input, "start")

                if tu.name == "memory" and not self.skip_memory:
                    result = self._memory_manager.route_tool_call(tu.name, tu.input)
                else:
                    result = handle_function_call(
                        function_name=tu.name,
                        function_args=tu.input,
                        task_id=task_id,
                        tool_call_id=tu.id,
                        session_id=self.session_id,
                        plugin_manager=self.plugin_manager,
                    )

                if self.tool_progress_callback:
                    self.tool_progress_callback(tu.name, tu.input, "complete")

                # Content must be a string, never None
                tool_result = result if result else ""
                self._conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tu.id,
                    "content": tool_result,
                })

            # Continue loop — recurse
            api_call_count += 1
            self._budget.consume()
            if api_call_count < self.max_iterations and self._budget.remaining > 0:
                return self._run_loop(tools, task_id)

        # Final response
        self._conversation_history.append({"role": "assistant", "content": text_content})
        if self.session_db:
            self.session_db.append_message(self.session_id, "assistant", text_content)# type: ignore[arg-type]

        logger.debug("End of _process_anthropic_response")
        return {
            "final_response": text_content,
            "messages": self._conversation_history,
            "session_id": self.session_id,
        }

    def interrupt(self) -> None:
        """Request graceful interruption of the current turn."""
        logger.debug("Beginning of interrupt")
        with self._interrupt_lock:
            self._interrupt_requested = True
        logger.debug("End of interrupt")

    def reset_interrupt(self) -> None:
        logger.debug("Beginning of reset_interrupt")
        with self._interrupt_lock:
            self._interrupt_requested = False
        logger.debug("End of reset_interrupt")

    @property
    def budget(self) -> IterationBudget:
        return self._budget

    @property
    def conversation_history(self) -> list[dict]:
        return self._conversation_history

    @conversation_history.setter
    def conversation_history(self, value: list[dict]) -> None:
        self._conversation_history = value
