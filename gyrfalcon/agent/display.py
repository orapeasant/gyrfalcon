"""Display helpers — KawaiiSpinner and formatting utilities."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("display")


_DEFAULT_FACES = ["(◕‿◕)", "(≧▽≦)", "(◠‿◠)", "(✿◠‿◠)", "(─‿─)", "(◕ᴗ◕)"]
_DEFAULT_VERBS = ["Thinking", "Processing", "Working", "Analyzing", "Computing"]


class KawaiiSpinner:
    """Animated terminal spinner with kawaii faces and customizable verbs.

    Uses a background thread for smooth animation without blocking.
    """

    def __init__(
        self,
        *,
        faces: list[str] | None = None,
        verbs: list[str] | None = None,
        interval: float = 0.3,
        stream: Any = None,
    ) -> None:
        """Initialize the spinner.

        Args:
            faces: List of face strings to cycle through.
            verbs: List of verb strings to cycle through.
            interval: Seconds between frame updates.
            stream: Output stream (defaults to sys.stderr).
        """
        self.faces = faces or list(_DEFAULT_FACES)
        self.verbs = verbs or list(_DEFAULT_VERBS)
        self.interval = interval
        self._stream = stream or sys.stderr
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._face_index = 0
        self._current_verb: str = self.verbs[0] if self.verbs else "Working"
        self._stop_event = threading.Event()

    @property
    def is_running(self) -> bool:
        """Whether the spinner is currently active."""
        return self._running

    def start(self) -> None:
        """Start the spinner animation in a background thread."""
        logger.debug("Beginning of start")
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._animate, daemon=True, name="kawaii-spinner"
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the spinner and clear the line."""
        logger.debug("Beginning of stop")
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        # Clear the spinner line
        self._clear_line()

    def update_verb(self, verb: str) -> None:
        """Update the current action verb displayed by the spinner.

        Args:
            verb: The new verb to display (e.g., "Searching").
        """
        logger.debug("Beginning of update_verb")
        with self._lock:
            self._current_verb = verb

    def get_frame(self) -> str:
        """Get the current spinner frame string.

        Returns:
            A formatted string like "(◕‿◕) Thinking..."
        """
        logger.debug("Beginning of get_frame")
        with self._lock:
            face = self.faces[self._face_index % len(self.faces)]
            verb = self._current_verb
        return f"{face} {verb}..."

    def _animate(self) -> None:
        """Animation loop running in the background thread."""
        logger.debug("Beginning of _animate")
        while not self._stop_event.is_set():
            frame = self.get_frame()
            self._write_frame(frame)
            with self._lock:
                self._face_index += 1
            self._stop_event.wait(timeout=self.interval)
        self._clear_line()

    def _write_frame(self, frame: str) -> None:
        """Write a frame to the output stream."""
        logger.debug("Beginning of _write_frame")
        try:
            self._stream.write(f"\r{frame}  ")
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def _clear_line(self) -> None:
        """Clear the current terminal line."""
        logger.debug("Beginning of _clear_line")
        try:
            self._stream.write("\r\033[K")
            self._stream.flush()
        except (OSError, ValueError):
            pass

    def __enter__(self) -> KawaiiSpinner:
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


def format_tool_call(name: str, args: dict[str, Any] | str | None = None) -> str:
    """Format a tool call for pretty-printing in the terminal.

    Args:
        name: Tool/function name.
        args: Arguments dict or string representation.

    Returns:
        A compact, readable representation like:
        ⚙ tool_name(arg1="val1", arg2=123)
    """
    logger.debug("Beginning of format_tool_call")
    if args is None:
        return f"⚙ {name}()"

    if isinstance(args, str):
        # Truncate long string args
        display = args if len(args) <= 80 else args[:77] + "..."
        return f"⚙ {name}({display})"

    # Dict args — format as key=value pairs
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 40:
            value = value[:37] + "..."
        parts.append(f'{key}="{value}"' if isinstance(value, str) else f"{key}={value}")

    args_str = ", ".join(parts)
    if len(args_str) > 120:
        args_str = args_str[:117] + "..."

    return f"⚙ {name}({args_str})"


def format_token_usage(input_tokens: int, output_tokens: int) -> str:
    """Format token usage counts for display.

    Args:
        input_tokens: Number of input/prompt tokens.
        output_tokens: Number of output/completion tokens.

    Returns:
        A formatted string like: "⟦ 1.2k in / 340 out ⟧"
    """
    logger.debug("Beginning of format_token_usage")
    def _fmt(n: int) -> str:
        logger.debug("Beginning of _fmt")
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}k"
        return str(n)

    total = input_tokens + output_tokens
    return f"⟦ {_fmt(input_tokens)} in / {_fmt(output_tokens)} out | {_fmt(total)} total ⟧"
