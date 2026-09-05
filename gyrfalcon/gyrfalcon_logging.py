"""Logging setup with rotation, session tagging, and secret redaction.

Log levels (from most verbose to least):
  TRACE     (5)  - Low-level tracing: function entry/exit, variable values
  DEBUG     (10) - Standard debug: internal state, decisions
  STATEMENT (15) - High-level statements: API calls, tool invocations, user actions
  INFO      (20) - Normal operational messages
  WARNING   (30) - Something unexpected but recoverable
  ERROR     (40) - Failures that need attention
  CRITICAL  (50) - Fatal errors

Set via GYRFALCON_LOG_LEVEL in ~/.gyrfalcon/.env or environment.
"""

import logging
import logging.handlers
import re
import os
import threading
from pathlib import Path

from gyrfalcon.gyrfalcon_constants import get_logs_dir, get_config_path

# --- Custom log levels ---
TRACE = 5
STATEMENT = 15

logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(STATEMENT, "STATEMENT")


def _logger_trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE):
        self._log(TRACE, message, args, **kwargs)


def _logger_statement(self, message, *args, **kwargs):
    if self.isEnabledFor(STATEMENT):
        self._log(STATEMENT, message, args, **kwargs)


logging.Logger.trace = _logger_trace
logging.Logger.statement = _logger_statement

# --- Level name mapping ---
_LEVEL_MAP = {
    "TRACE": TRACE,
    "DEBUG": logging.DEBUG,
    "STATEMENT": STATEMENT,
    "PROCEDURE": STATEMENT,  # alias
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9]{20,})"),
    re.compile(r"(key-[a-zA-Z0-9]{20,})"),
    re.compile(r"(ghp_[a-zA-Z0-9]{36,})"),
    re.compile(r"(ghu_[a-zA-Z0-9]{36,})"),
    re.compile(r"(AKIA[A-Z0-9]{16})"),
]

_session_local = threading.local()


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs API keys/tokens from output and sanitises non-ASCII for cp1252 streams."""

    def format(self, record: logging.LogRecord) -> str:
        record.session_tag = getattr(_session_local, "tag", "")
        msg = super().format(record)
        for pattern in _SECRET_PATTERNS:
            msg = pattern.sub("[REDACTED]", msg)
        # Replace common non-ASCII symbols that crash cp1252 Windows terminals
        msg = msg.replace("→", "->").replace("←", "<-").replace("⠋", "~")
        # Final safety net: encode/decode with replacement so nothing can crash the handler
        msg = msg.encode("cp1252", errors="replace").decode("cp1252")
        return msg


def set_session_tag(tag: str) -> None:
    """Set the current session tag for log records (thread-local)."""
    _session_local.tag = tag


def _resolve_log_level(verbose: bool = False) -> int:
    """Resolve log level: env var → config.yaml → verbose flag → ERROR (default)."""
    # 1. Env var takes highest priority (useful for one-off debugging)
    env_level = os.environ.get("GYRFALCON_LOG_LEVEL", "").strip().upper()
    if env_level:
        level = _LEVEL_MAP.get(env_level)
        if level is not None:
            return level
        try:
            return int(env_level)
        except ValueError:
            pass

    # 2. Config file — read YAML directly to avoid circular import with config.py
    try:
        import yaml as _yaml
        config_path = get_config_path()
        if config_path.exists():
            with open(config_path) as _f:
                _cfg = _yaml.safe_load(_f) or {}
            cfg_level = _cfg.get("logging", {}).get("level", "").strip().upper()
            if cfg_level:
                level = _LEVEL_MAP.get(cfg_level)
                if level is not None:
                    return level
                try:
                    return int(cfg_level)
                except ValueError:
                    pass
    except Exception:
        pass

    # 3. --verbose flag
    if verbose:
        return logging.DEBUG

    # 4. Default: ERROR only
    return logging.ERROR


def setup_logging(mode: str = "cli", verbose: bool = False) -> None:
    """Creates rotated log files under ~/.gyrfalcon/logs/ with session tagging and secret redaction."""
    logs_dir = get_logs_dir()

    log_format = "%(asctime)s [%(levelname)s] %(session_tag)s%(name)s: %(message)s"
    formatter = RedactingFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

    effective_level = _resolve_log_level(verbose)

    root_logger = logging.getLogger("gyrfalcon")
    root_logger.setLevel(effective_level)


    # Remove existing handlers
    root_logger.handlers.clear()

    # Agent log (all activity)
    agent_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "agent.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    agent_handler.setLevel(effective_level)
    agent_handler.setFormatter(formatter)
    root_logger.addHandler(agent_handler)

    # Error log (warnings+)
    error_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "errors.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # MCP log — all design-time MCP activity (store, discover, connect, API calls)
    # Captures: gyrfalcon.tools.mcp  and  gyrfalcon.mcp (used by web_server MCP endpoints)
    mcp_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "mcp.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    mcp_handler.setLevel(effective_level)
    mcp_handler.setFormatter(formatter)
    # Attach to both MCP-specific loggers (they propagate to root but also write here)
    for mcp_logger_name in ("gyrfalcon.tools.mcp", "gyrfalcon.mcp"):
        mcp_logger_inst = logging.getLogger(mcp_logger_name)
        mcp_logger_inst.addHandler(mcp_handler)

    # Gateway log (gateway mode only)
    if mode == "gateway":
        gw_handler = logging.handlers.RotatingFileHandler(
            logs_dir / "gateway.log", maxBytes=10 * 1024 * 1024, backupCount=5
        )
        gw_handler.setLevel(logging.INFO)
        gw_handler.setFormatter(formatter)
        logging.getLogger("gyrfalcon.gateway").addHandler(gw_handler)

    # Console handler when level is below INFO (TRACE/DEBUG/STATEMENT) or verbose
    # Set GYRFALCON_LOG_CONSOLE=false to suppress console output (file-only logging)
    console_enabled = os.environ.get("GYRFALCON_LOG_CONSOLE", "true").strip().lower() != "false"
    if console_enabled and (effective_level < logging.INFO or verbose):
        import sys, io
        # Force UTF-8 on Windows to avoid cp1252 UnicodeEncodeError
        if hasattr(sys.stdout, "buffer"):
            utf8_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        else:
            utf8_stream = sys.stdout
        console_handler = logging.StreamHandler(utf8_stream)
        console_handler.setLevel(effective_level)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)

    root_logger.info(f"xx logger level: {effective_level}")


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced logger."""
    return logging.getLogger(f"gyrfalcon.{name}")
