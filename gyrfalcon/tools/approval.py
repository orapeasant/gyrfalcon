"""Command safety — dangerous command detection and approval."""

from __future__ import annotations

import contextlib
import contextvars
import re
from typing import Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("approval")


# Never allowed, period — these are catastrophic or malicious
HARDLINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Linux/macOS destructive
    (re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+.*|.*-[a-zA-Z]*f[a-zA-Z]*)\s*/\s*$"), "Recursive delete of root filesystem"),
    (re.compile(r"rm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+.*|.*-[a-zA-Z]*f[a-zA-Z]*)\s*/[^a-zA-Z]"), "Recursive delete of root filesystem"),
    (re.compile(r"rm\s+-rf\s+/\s*$"), "Recursive delete of root filesystem"),
    (re.compile(r"rm\s+-rf\s+/\s+"), "Recursive delete of root filesystem"),
    (re.compile(r"rm\s+-rf\s+~\s*$"), "Recursive delete of home directory"),
    (re.compile(r"rm\s+-rf\s+\$HOME"), "Recursive delete of home directory"),
    (re.compile(r"rm\s+-rf\s+\*"), "Recursive delete of all files"),
    (re.compile(r"rm\s+-rf\s+\.\s*$"), "Recursive delete of current directory"),
    (re.compile(r"rm\s+-rf\s+\.\.\s*$"), "Recursive delete of parent directory"),
    (re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"), "Fork bomb"),
    (re.compile(r"\.\s*/dev/null\s*>\s"), "Destructive redirect"),
    (re.compile(r"mkfs\s"), "Filesystem format"),
    (re.compile(r"dd\s+.*of=/dev/[sh]d"), "Direct disk write"),
    (re.compile(r">\s*/dev/[sh]d"), "Direct disk overwrite"),
    (re.compile(r"chmod\s+-R\s+777\s+/\s*$"), "World-writable root"),
    (re.compile(r"echo\s.*>\s*/etc/passwd"), "Passwd overwrite"),
    (re.compile(r"echo\s.*>\s*/etc/shadow"), "Shadow overwrite"),
    (re.compile(r"cat\s*/dev/urandom\s*>\s*/dev"), "Random data to device"),
    # Windows destructive
    (re.compile(r"del\s+.*/[sfq].*[Cc]:", re.IGNORECASE), "Delete system drive"),
    (re.compile(r"del\s+.*[Cc]:\\\*", re.IGNORECASE), "Delete all files on system drive"),
    (re.compile(r"rd\s+.*/[sq].*[Cc]:", re.IGNORECASE), "Remove system drive directory tree"),
    (re.compile(r"rmdir\s+.*/[sq].*[Cc]:", re.IGNORECASE), "Remove system drive directory tree"),
    (re.compile(r"format\s+[Cc]:", re.IGNORECASE), "Format system drive"),
    (re.compile(r"del\s+.*/[sfq].*\*", re.IGNORECASE), "Delete all files recursively"),
    (re.compile(r"rd\s+.*/[sq].*\\\\", re.IGNORECASE), "Remove network path tree"),
    # Credential/data exfiltration
    (re.compile(r"curl\s+.*[-d].*password.*\w+\.\w+"), "Credential exfiltration"),
    (re.compile(r"wget\s+.*--post.*(password|token|secret)", re.IGNORECASE), "Credential exfiltration"),
    (re.compile(r"cat\s+.*(/etc/shadow|\.ssh/id_)", re.IGNORECASE), "Reading sensitive credentials"),
    # Obfuscation/injection attempts
    (re.compile(r"\$\{[^}]*@P\}"), "Shell expansion exploit (parameter transformation)"),
    (re.compile(r"eval\s+.*\$\{"), "Eval with variable expansion"),
    (re.compile(r"base64\s+-d.*\|\s*(ba)?sh"), "Decoded payload execution"),
    (re.compile(r"python.*-c.*exec\(.*base64"), "Encoded Python execution"),
    (re.compile(r"echo\s+[A-Za-z0-9+/=]{20,}\s*\|\s*base64\s+-d\s*\|\s*(ba)?sh"), "Encoded shell execution"),
]

# Require user approval
DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"rm\s+-rf\s"), "Recursive force delete"),
    (re.compile(r"rm\s+-r\s"), "Recursive delete"),
    (re.compile(r"rm\s+.*\*"), "Wildcard delete"),
    (re.compile(r"chmod\s+777"), "World-writable permissions"),
    (re.compile(r"chmod\s+-R"), "Recursive permission change"),
    (re.compile(r"chown\s+-R"), "Recursive ownership change"),
    (re.compile(r"curl\s.*\|\s*(ba)?sh"), "Pipe from internet to shell"),
    (re.compile(r"wget\s.*\|\s*(ba)?sh"), "Pipe from internet to shell"),
    (re.compile(r"curl\s.*\|\s*python"), "Pipe from internet to python"),
    (re.compile(r"pip\s+install\s"), "Package installation"),
    (re.compile(r"npm\s+install\s+-g"), "Global npm package installation"),
    (re.compile(r"apt(-get)?\s+install"), "System package installation"),
    (re.compile(r"apt(-get)?\s+(remove|purge)"), "System package removal"),
    (re.compile(r"yum\s+(install|remove)"), "System package management"),
    (re.compile(r"systemctl\s+(stop|restart|disable)"), "Service management"),
    (re.compile(r"docker\s+rm"), "Docker container removal"),
    (re.compile(r"docker\s+system\s+prune"), "Docker system prune"),
    (re.compile(r"git\s+push\s+.*--force"), "Force push"),
    (re.compile(r"git\s+reset\s+--hard"), "Hard reset"),
    (re.compile(r"git\s+clean\s+-[a-z]*f"), "Git clean force"),
    (re.compile(r"sudo\s"), "Elevated privileges"),
    (re.compile(r"su\s+-?\s*$"), "Switch to root user"),
    (re.compile(r"shutdown|reboot|poweroff"), "System shutdown/reboot"),
    (re.compile(r"kill\s+-9\s+1\b"), "Kill init process"),
    (re.compile(r"killall\s"), "Kill processes by name"),
    (re.compile(r"pkill\s+-9"), "Force kill processes"),
    (re.compile(r"iptables\s"), "Firewall modification"),
    (re.compile(r"ufw\s"), "Firewall modification"),
    (re.compile(r"crontab\s+-r"), "Remove all scheduled jobs"),
    (re.compile(r"mv\s+.*/etc/"), "Moving system config files"),
    (re.compile(r"truncate\s"), "File truncation"),
    (re.compile(r"shred\s"), "Secure file deletion"),
    # Windows
    (re.compile(r"del\s+/[sfq]", re.IGNORECASE), "Forced/quiet file deletion"),
    (re.compile(r"reg\s+delete", re.IGNORECASE), "Registry deletion"),
    (re.compile(r"net\s+stop", re.IGNORECASE), "Stop Windows service"),
    (re.compile(r"taskkill\s+/f", re.IGNORECASE), "Force kill process"),
]


def detect_dangerous_command(command: str) -> Optional[str]:
    """
    Pattern-match against HARDLINE_PATTERNS (never-allow) and DANGEROUS_PATTERNS (require approval).
    
    Returns:
        None if safe
        "BLOCKED: <reason>" if hardline match
        "APPROVAL_REQUIRED: <reason>" if dangerous match
    """
    logger.debug("Beginning of detect_dangerous_command")
    command_stripped = command.strip()

    # Check hardline patterns first
    for pattern, reason in HARDLINE_PATTERNS:
        if pattern.search(command_stripped):
            return f"BLOCKED: {reason}"

    # Check dangerous patterns
    for pattern, reason in DANGEROUS_PATTERNS:
        if pattern.search(command_stripped):
            return f"APPROVAL_REQUIRED: {reason}"

    return None


def is_safe_command(command: str) -> bool:
    """Quick check if command is safe (no approval needed)."""
    logger.debug("Beginning of is_safe_command")
    return detect_dangerous_command(command) is None


def requires_approval(command: str) -> bool:
    """Check if command requires user approval."""
    logger.debug("Beginning of requires_approval")
    result = detect_dangerous_command(command)
    return result is not None and result.startswith("APPROVAL_REQUIRED:")


def is_blocked(command: str) -> bool:
    """Check if command is unconditionally blocked."""
    logger.debug("Beginning of is_blocked")
    result = detect_dangerous_command(command)
    return result is not None and result.startswith("BLOCKED:")


# ── One-shot grants ──────────────────────────────────────────────────────────
# A command a person has just approved, for the single re-run that follows.
# Scoped to the calling context rather than stored, so it cannot leak into
# another turn, another thread, or a later call with the same text.
_APPROVED: contextvars.ContextVar[frozenset] = contextvars.ContextVar(
    "gyrfalcon_approved_commands", default=frozenset()
)


@contextlib.contextmanager
def approved_for_this_call(command: str):
    """Run the block with `command` already approved. Hardline-blocked commands
    are unaffected — those are refused whatever anyone says."""
    token = _APPROVED.set(_APPROVED.get() | {command})
    try:
        yield
    finally:
        _APPROVED.reset(token)


# ── Session-level allow_all flag ──────────────────────────────────────────────
# Maps session_id → True when the user has invoked /allow-all
_allow_all_sessions: set[str] = set()
_allow_all_lock = __import__("threading").Lock()


def set_allow_all(session_id: str, enabled: bool = True) -> None:
    """Grant or revoke allow-all for a session."""
    with _allow_all_lock:
        if enabled:
            _allow_all_sessions.add(session_id)
        else:
            _allow_all_sessions.discard(session_id)


def is_allow_all(session_id: str | None) -> bool:
    """Return True if this session has /allow-all active."""
    if not session_id:
        return False
    with _allow_all_lock:
        return session_id in _allow_all_sessions


def needs_approval_for_tool(
    tool_name: str,
    command: str | None = None,
    session_id: str | None = None,
) -> tuple[bool, str]:
    """Central decision point for whether a tool call needs user approval.

    Returns (needs_approval: bool, reason: str).

    Rules (in order):
      1. If session has /allow-all → never ask (except BLOCKED commands).
      2. If the tool is registered as read_only=True → never ask.
      3. If command string matches HARDLINE_PATTERNS → block unconditionally.
      4. If command string matches DANGEROUS_PATTERNS AND approval_mode != "none" → ask.
      5. Otherwise → auto-approve.
    """
    from gyrfalcon.config import cfg_get
    from gyrfalcon.tools import registry as _reg

    # Check command-level blocks first (these can never be bypassed)
    if command:
        danger = detect_dangerous_command(command)
        if danger and danger.startswith("BLOCKED:"):
            return True, danger    # unconditional block — still "needs approval" (denied)

    # A command someone just approved, for this one re-run.
    if command and command in _APPROVED.get():
        return False, ""

    # allow-all bypasses everything except BLOCKED
    if is_allow_all(session_id):
        return False, ""

    # Read-only tool — no approval needed
    if _reg.is_read_only(tool_name):
        return False, ""

    # Check command-level dangerous patterns
    if command:
        danger = detect_dangerous_command(command)
        if danger and danger.startswith("APPROVAL_REQUIRED:"):
            approval_mode = cfg_get("security.approval_mode", "smart")
            if approval_mode != "none":
                return True, danger

    return False, ""
