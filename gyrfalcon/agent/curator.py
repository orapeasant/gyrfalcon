"""Conversation trajectory curator — manages when and how to compress/summarize."""

from __future__ import annotations

from typing import Any

from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.agent.auxiliary_client import call_llm

logger = get_logger("agent.curator")

_DEFAULT_SUMMARY_BUDGET = 2000
_CHARS_PER_TOKEN = 4


class Curator:
    """Manages conversation trajectory curation and compression decisions.

    Determines when a conversation has grown too long and orchestrates
    compression via the auxiliary LLM client.
    """

    def __init__(
        self,
        *,
        default_budget: int = _DEFAULT_SUMMARY_BUDGET,
        compression_ratio: float = 0.25,
        min_messages_to_compress: int = 6,
    ) -> None:
        self.default_budget = default_budget
        self.compression_ratio = compression_ratio
        self.min_messages_to_compress = min_messages_to_compress
        self._total_input_tokens: int = 0
        self._total_output_tokens: int = 0
        self._compression_count: int = 0

    @property
    def token_usage(self) -> dict[str, int]:
        """Return cumulative token usage from curation calls."""
        return {
            "input_tokens": self._total_input_tokens,
            "output_tokens": self._total_output_tokens,
            "compression_count": self._compression_count,
        }

    def should_compress(self, messages: list[dict[str, Any]], max_tokens: int) -> bool:
        """Determine whether the conversation should be compressed.

        Args:
            messages: The full conversation message list.
            max_tokens: The maximum token budget for the context window.

        Returns:
            True if estimated token count exceeds the threshold.
        """
        logger.debug("Beginning of should_compress")
        if len(messages) < self.min_messages_to_compress:
            return False
        estimated = self._estimate_tokens(messages)
        threshold = int(max_tokens * (1.0 - self.compression_ratio))
        return estimated > threshold

    def curate(
        self,
        messages: list[dict[str, Any]],
        budget: int | None = None,
    ) -> list[dict[str, Any]]:
        """Curate a conversation by summarizing older messages.

        Protects the first message (system prompt) and recent tail, then
        summarizes the middle section using the auxiliary LLM.

        Args:
            messages: Full conversation history.
            budget: Max tokens for the summary output.

        Returns:
            A curated (shorter) message list with a summary injected.
        """
        logger.debug("Beginning of curate")
        if budget is None:
            budget = self.default_budget

        if len(messages) < self.min_messages_to_compress:
            return messages

        # Protect head (system + first user turn) and tail
        protect_head = min(2, len(messages))
        protect_tail = max(3, len(messages) // 4)
        tail_start = len(messages) - protect_tail

        if tail_start <= protect_head:
            return messages

        head = messages[:protect_head]
        middle = messages[protect_head:tail_start]
        tail = messages[tail_start:]

        if not middle:
            return messages

        summary_text = self._generate_summary(middle, budget)
        self._compression_count += 1

        summary_message: dict[str, Any] = {
            "role": "system",
            "content": (
                f"[Curator Summary #{self._compression_count}] "
                f"The following summarizes {len(middle)} earlier messages:\n\n"
                f"{summary_text}"
            ),
        }

        logger.info(
            f"Curated {len(middle)} messages into summary "
            f"(curation #{self._compression_count})"
        )
        return head + [summary_message] + tail

    def reset(self) -> None:
        """Reset compression state for a new session."""
        logger.debug("Beginning of reset")
        self._compression_count = 0
        self._total_input_tokens = 0
        self._total_output_tokens = 0

    def _generate_summary(self, messages: list[dict[str, Any]], budget: int) -> str:
        """Generate a summary of the given messages via auxiliary LLM."""
        logger.debug("Beginning of _generate_summary")
        prompt_parts: list[str] = [
            "Summarize the following conversation segment concisely. "
            "Preserve: key decisions, file paths mentioned, tool results, "
            "and any unresolved questions.\n\n"
        ]
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                # Truncate very long individual messages for the summary prompt
                truncated = content[:1000] if len(content) > 1000 else content
                prompt_parts.append(f"[{role}]: {truncated}\n")
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls[:5]:
                    fn = tc.get("function", {})
                    prompt_parts.append(
                        f"[{role} tool_call]: {fn.get('name', '?')}(...)\n"
                    )

        prompt = "".join(prompt_parts)

        try:
            response = call_llm(
                task="curation",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=budget,
                temperature=0.3,
            )
            if response and hasattr(response, "choices"):
                choice = response.choices[0]
                # Track token usage if available
                if hasattr(response, "usage") and response.usage:
                    self._total_input_tokens += getattr(
                        response.usage, "prompt_tokens", 0
                    )
                    self._total_output_tokens += getattr(
                        response.usage, "completion_tokens", 0
                    )
                return choice.message.content or ""
        except Exception as e:
            logger.warning(f"LLM curation failed, using extractive fallback: {e}")

        # Extractive fallback
        return self._extractive_summary(messages)

    def _extractive_summary(self, messages: list[dict[str, Any]]) -> str:
        """Simple extractive fallback when LLM is unavailable."""
        logger.debug("Beginning of _extractive_summary")
        parts: list[str] = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str) and content and role in ("user", "assistant"):
                parts.append(f"[{role}]: {content[:150]}")
            if len(parts) >= 15:
                break
        return "\n".join(parts)

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
        """Rough token estimate based on character count."""
        logger.debug("Beginning of _estimate_tokens")
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(content) // _CHARS_PER_TOKEN
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += len(part.get("text", "")) // _CHARS_PER_TOKEN
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                total += sum(len(str(tc)) // _CHARS_PER_TOKEN for tc in tool_calls)
        return total
