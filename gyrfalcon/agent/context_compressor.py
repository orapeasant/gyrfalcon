"""Context compression — auto-summarization when approaching token limits."""

from __future__ import annotations

from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("agent.compressor")

_SUMMARY_RATIO = 0.20
_IMAGE_TOKEN_ESTIMATE = 1600


class ContextCompressor:
    """Manages context window compression when conversations approach token limits."""

    def __init__(
        self,
        model: str = "",
        context_length: int = 128_000,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_tail_tokens: int = 20_000,
    ):
        self.model = model
        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_tail_tokens = protect_tail_tokens
        self._compression_count = 0

    def should_compress(self, messages: list[dict], system_message: str = "") -> bool:
        """Check if token count exceeds threshold."""
        logger.debug("Beginning of should_compress")
        estimated = self._estimate_tokens(messages, system_message)
        threshold = int(self.context_length * self.threshold_percent)
        return estimated > threshold

    def compress(
        self,
        messages: list[dict],
        system_message: str = "",
        task_id: str | None = None,
        focus_topic: str | None = None,
        auxiliary_client=None,
    ) -> list[dict]:
        """Compress conversation history. Returns new message list."""
        logger.debug("Beginning of compress")
        if len(messages) <= self.protect_first_n + 2:
            return messages

        # Step 1: Prune old tool results
        messages = self._prune_tool_results(messages)

        # Step 2: Protect head
        head = messages[:self.protect_first_n]

        # Step 3: Protect tail (by token budget)
        tail_start = self._find_tail_start(messages)
        tail = messages[tail_start:]

        # Step 4: Middle section to summarize
        middle = messages[self.protect_first_n:tail_start]
        if not middle:
            return messages

        # Step 5: Generate summary
        summary = self._summarize_middle(middle, focus_topic, auxiliary_client)

        self._compression_count += 1
        logger.info(
            f"Compressed {len(middle)} messages into summary "
            f"(compression #{self._compression_count})"
        )

        # Reconstruct
        summary_msg = {
            "role": "system",
            "content": f"[Context Summary #{self._compression_count}]\n\n{summary}",
        }
        return head + [summary_msg] + tail

    def update_model(self, model: str, context_length: int) -> None:
        logger.debug("Beginning of update_model")
        self.model = model
        self.context_length = context_length

    def on_session_reset(self) -> None:
        logger.debug("Beginning of on_session_reset")
        self._compression_count = 0

    def _estimate_tokens(self, messages: list[dict], system_message: str = "") -> int:
        """Rough token estimate for messages."""
        logger.debug("Beginning of _estimate_tokens")
        total = len(system_message) // 4
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // 4
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") == "text":
                            total += len(part.get("text", "")) // 4
                        elif part.get("type") == "image_url":
                            total += _IMAGE_TOKEN_ESTIMATE
            # Tool calls
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                total += sum(len(str(tc)) // 4 for tc in tool_calls)
        return total

    def _prune_tool_results(self, messages: list[dict]) -> list[dict]:
        """Truncate verbose old tool outputs."""
        logger.debug("Beginning of _prune_tool_results")
        result = []
        total = len(messages)
        for i, msg in enumerate(messages):
            if msg.get("role") == "tool" and i < total - 10:
                content = msg.get("content", "")
                if isinstance(content, str) and len(content) > 2000:
                    msg = {**msg, "content": content[:2000] + "\n... [pruned]"}
            result.append(msg)
        return result

    def _find_tail_start(self, messages: list[dict]) -> int:
        """Find where tail section starts (last ~20K tokens)."""
        logger.debug("Beginning of _find_tail_start")
        tokens = 0
        for i in range(len(messages) - 1, self.protect_first_n - 1, -1):
            msg = messages[i]
            content = msg.get("content", "")
            if isinstance(content, str):
                tokens += len(content) // 4
            if tokens > self.protect_tail_tokens // 4:
                return i
        return max(self.protect_first_n, len(messages) - 5)

    def _summarize_middle(
        self, middle: list[dict], focus_topic: str | None, auxiliary_client=None
    ) -> str:
        """Summarize middle messages. Uses LLM if available, else extractive."""
        logger.debug("Beginning of _summarize_middle")
        if auxiliary_client:
            try:
                prompt = "Summarize this conversation segment concisely, preserving key decisions, facts, and tool results:\n\n"
                for msg in middle:
                    role = msg.get("role", "?")
                    content = msg.get("content", "")
                    if isinstance(content, str) and content:
                        prompt += f"[{role}]: {content[:500]}\n"

                if focus_topic:
                    prompt += f"\nFocus especially on: {focus_topic}"

                response = auxiliary_client.call_llm(
                    task="compression",
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                )
                if response and hasattr(response, "choices"):
                    return response.choices[0].message.content
            except Exception as e:
                logger.warning(f"LLM summarization failed: {e}")

        # Fallback: extractive summary
        summary_parts = []
        for msg in middle:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and content and role in ("user", "assistant"):
                summary_parts.append(f"[{role}]: {content[:200]}")
        return "\n".join(summary_parts[:20])
