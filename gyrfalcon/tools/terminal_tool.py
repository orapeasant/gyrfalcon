"""Terminal tool — shell command execution."""

from __future__ import annotations

import json
import os
import subprocess
import signal
import threading
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.tools.approval import detect_dangerous_command, needs_approval_for_tool
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.config import cfg_get

logger = get_logger("tools.terminal")

_background_processes: dict[str, subprocess.Popen] = {}
_process_lock = threading.Lock()


def terminal_tool(args: dict, **kwargs) -> str:
    """Execute shell commands."""
    logger.debug("Beginning of terminal_tool")
    command = args.get("command", "")
    background = args.get("background", False)
    timeout = args.get("timeout", 120)
    workdir = args.get("workdir")
    notify_on_complete = args.get("notify_on_complete", False)
    session_id = kwargs.get("session_id")

    if not command:
        return json.dumps({"error": "No command provided"})

    # Safety check — respects allow_all and read-only flags
    needs_appr, reason = needs_approval_for_tool(
        tool_name="terminal",
        command=command,
        session_id=session_id,
    )
    if needs_appr:
        if reason.startswith("BLOCKED:"):
            return json.dumps({"error": reason})
        return json.dumps({
            "approval_required": True,
            "reason": reason,
            "command": command,
        })

    # Resolve working directory
    cwd = workdir or cfg_get("terminal.cwd") or os.getcwd()

    # Execute
    backend = cfg_get("terminal.backend", "local")
    if backend == "local":
        return _execute_local(command, cwd, timeout, background, kwargs.get("task_id"))
    else:
        return json.dumps({"error": f"Backend '{backend}' not yet implemented. Use 'local'."})


def _execute_local(
    command: str, cwd: str, timeout: int, background: bool, task_id: str | None
) -> str:
    """Execute command locally via subprocess."""
    logger.debug("Beginning of _execute_local")
    try:
        if background:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            pid = process.pid
            with _process_lock:
                _background_processes[str(pid)] = process

            return json.dumps({
                "status": "background",
                "pid": pid,
                "message": f"Process started in background with PID {pid}",
            })

        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"

        # Truncate large outputs
        if len(output) > 50_000:
            output = output[:50_000] + "\n... [output truncated at 50KB]"

        return json.dumps({
            "exit_code": result.returncode,
            "output": output.strip(),
        })

    except subprocess.TimeoutExpired:
        return json.dumps({
            "error": f"Command timed out after {timeout}s",
            "command": command,
        })
    except OSError as e:
        return json.dumps({"error": f"Execution failed: {str(e)}"})


# Register tool
registry.register(
    name="terminal",
    toolset="terminal",
    schema={
        "name": "terminal",
        "description": "Execute shell commands. Use for running programs, scripts, build commands, git operations, and system tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background (non-blocking)",
                    "default": False,
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120)",
                    "default": 120,
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory (default: current)",
                },
            },
            "required": ["command"],
        },
    },
    handler=terminal_tool,
    emoji="💻",
)
