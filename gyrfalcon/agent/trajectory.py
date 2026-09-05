"""Trajectory: agent step reconstruction from session history.

`build_trajectory` is the live path — it reshapes the flat `messages` rows that
SessionDB already records into the ordered LLM-call → tool-call → result
structure a transcript view flattens away. It reads nothing from disk itself.

The JSONL helpers below are a separate, currently unused persistence layer.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("agent.trajectory")

_TRAJECTORIES_DIR = Path.home() / ".gyrfalcon" / "trajectories"


def _parse_tool_calls(raw: Any) -> list[dict[str, Any]]:
    """Normalize the stored tool_calls JSON into a list of dicts."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Unparseable tool_calls payload; skipping")
        return []
    return parsed if isinstance(parsed, list) else []


def _tool_call_fields(call: dict[str, Any]) -> tuple[str, str, Any]:
    """Extract (id, name, arguments) from either OpenAI or flat tool-call shapes."""
    fn = call.get("function") or {}
    name = fn.get("name") or call.get("name") or "unknown"
    raw_args = fn.get("arguments", call.get("arguments"))
    args: Any = raw_args
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args)
        except (TypeError, ValueError):
            args = raw_args  # leave the raw string; it is still worth showing
    return call.get("id") or "", name, args


def build_trajectory(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Group flat session messages into ordered trajectory steps.

    Each assistant tool call is paired with the tool-result row carrying the
    matching tool_call_id, so results render under the call that produced them
    instead of as loose sibling messages.
    """
    logger.debug("Beginning of build_trajectory")

    # Index tool results by the call they answer; anything unmatched is kept
    # so a result whose call was compressed away is not silently dropped.
    results_by_id: dict[str, dict[str, Any]] = {}
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            results_by_id[msg["tool_call_id"]] = msg

    matched_ids: set[str] = set()
    steps: list[dict[str, Any]] = []
    tool_call_count = 0

    for msg in messages:
        role = msg.get("role", "unknown")
        if role == "tool":
            continue  # folded into its originating call below

        step: dict[str, Any] = {
            "index": len(steps),
            "role": role,
            "content": msg.get("content"),
            "at": msg.get("created_at"),
        }
        if msg.get("reasoning"):
            step["reasoning"] = msg["reasoning"]

        calls = []
        for call in _parse_tool_calls(msg.get("tool_calls")):
            call_id, name, args = _tool_call_fields(call)
            entry: dict[str, Any] = {"id": call_id, "name": name, "arguments": args}

            result = results_by_id.get(call_id)
            if result:
                matched_ids.add(call_id)
                entry["result"] = result.get("content")
                entry["result_at"] = result.get("created_at")
                started, ended = msg.get("created_at"), result.get("created_at")
                if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
                    entry["duration"] = round(max(ended - started, 0.0), 3)
            calls.append(entry)
            tool_call_count += 1

        if calls:
            step["tool_calls"] = calls
        steps.append(step)

    orphans = [
        {
            "index": len(steps) + i,
            "role": "tool",
            "content": m.get("content"),
            "at": m.get("created_at"),
            "tool_name": m.get("tool_name"),
            "orphaned": True,
        }
        for i, m in enumerate(
            m for m in messages
            if m.get("role") == "tool" and m.get("tool_call_id") not in matched_ids
        )
    ]
    steps.extend(orphans)

    return {
        "steps": steps,
        "step_count": len(steps),
        "tool_call_count": tool_call_count,
        "message_count": len(messages),
    }


def _ensure_dir() -> Path:
    """Ensure the trajectories directory exists."""
    logger.debug("Beginning of _ensure_dir")
    _TRAJECTORIES_DIR.mkdir(parents=True, exist_ok=True)
    return _TRAJECTORIES_DIR


def _trajectory_path(session_id: str) -> Path:
    """Return the path for a given session trajectory file."""
    logger.debug("Beginning of _trajectory_path")
    return _ensure_dir() / f"{session_id}.jsonl"


def save_turn(session_id: str, turn_data: dict[str, Any]) -> None:
    """Append a single turn to the session trajectory file.

    Each turn is stored as a single JSON line with at minimum:
    role, content, tool_calls (nullable), and timestamp.

    Args:
        session_id: Unique session identifier.
        turn_data: Dict with keys: role, content, and optionally tool_calls.
    """
    logger.debug("Beginning of save_turn")
    record: dict[str, Any] = {
        "role": turn_data.get("role", "unknown"),
        "content": turn_data.get("content"),
        "tool_calls": turn_data.get("tool_calls"),
        "timestamp": turn_data.get("timestamp") or time.time(),
    }

    # Preserve any extra metadata the caller provides
    for key in turn_data:
        if key not in record:
            record[key] = turn_data[key]

    path = _trajectory_path(session_id)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.error(f"Failed to save turn for session {session_id}: {e}")
        raise


def load_trajectory(session_id: str) -> list[dict[str, Any]]:
    """Load the full conversation trajectory for a session.

    Args:
        session_id: Unique session identifier.

    Returns:
        List of turn dicts in chronological order.

    Raises:
        FileNotFoundError: If no trajectory exists for the session.
    """
    logger.debug("Beginning of load_trajectory")
    path = _trajectory_path(session_id)
    if not path.exists():
        raise FileNotFoundError(f"No trajectory found for session: {session_id}")

    turns: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                turns.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(
                    f"Skipping malformed line {line_num} in {session_id}: {e}"
                )
    return turns


def list_trajectories() -> list[str]:
    """List all available session IDs that have saved trajectories.

    Returns:
        List of session ID strings (without .jsonl extension).
    """
    logger.debug("Beginning of list_trajectories")
    directory = _ensure_dir()
    return sorted(
        p.stem for p in directory.glob("*.jsonl") if p.is_file()
    )


def delete_trajectory(session_id: str) -> bool:
    """Delete a trajectory file.

    Args:
        session_id: Session to delete.

    Returns:
        True if deleted, False if not found.
    """
    logger.debug("Beginning of delete_trajectory")
    path = _trajectory_path(session_id)
    if path.exists():
        path.unlink()
        logger.info(f"Deleted trajectory: {session_id}")
        return True
    return False


class TrajectoryReplayer:
    """Replay a saved trajectory turn-by-turn.

    Useful for debugging, testing, or re-running a conversation
    through a different model.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self._turns = load_trajectory(session_id)
        self._index = 0

    @property
    def total_turns(self) -> int:
        """Total number of turns in the trajectory."""
        return len(self._turns)

    @property
    def current_index(self) -> int:
        """Current replay position."""
        return self._index

    @property
    def is_complete(self) -> bool:
        """Whether all turns have been replayed."""
        return self._index >= len(self._turns)

    def next_turn(self) -> dict[str, Any] | None:
        """Get the next turn in the trajectory.

        Returns:
            The next turn dict, or None if replay is complete.
        """
        logger.debug("Beginning of next_turn")
        if self._index >= len(self._turns):
            return None
        turn = self._turns[self._index]
        self._index += 1
        return turn

    def peek(self) -> dict[str, Any] | None:
        """Peek at the next turn without advancing."""
        logger.debug("Beginning of peek")
        if self._index >= len(self._turns):
            return None
        return self._turns[self._index]

    def reset(self) -> None:
        """Reset replay to the beginning."""
        logger.debug("Beginning of reset")
        self._index = 0

    def get_messages_up_to(self, index: int) -> list[dict[str, Any]]:
        """Get all turns up to (but not including) the given index.

        Useful for reconstructing conversation state at a point in time.
        """
        logger.debug("Beginning of get_messages_up_to")
        return self._turns[:index]
