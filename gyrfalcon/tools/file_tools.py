"""File tools — read, write, patch, search."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from gyrfalcon.tools import registry
from gyrfalcon.tools.restrictions import sensitive_read_reason
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.file")

WRITE_DENIED_PATHS = {
    os.path.expanduser("~/.ssh/authorized_keys"),
    "/etc/shadow",
    "/etc/passwd",
    "/etc/sudoers",
}

# Directories where write operations are blocked
WRITE_DENIED_PREFIXES = [
    "/etc/",
    "/boot/",
    "/usr/",
    "/sbin/",
    "/bin/",
    "/lib/",
    "/lib64/",
    "/sys/",
    "/proc/",
    "/dev/",
    "C:\\Windows\\",
    "C:\\Program Files\\",
    "C:\\Program Files (x86)\\",
]


def _is_path_denied(path: Path, for_write: bool = True) -> str | None:
    """Check if a path is denied for write operations. Returns reason or None."""
    logger.debug("Beginning of _is_path_denied")
    resolved = str(path)

    if resolved in WRITE_DENIED_PATHS:
        return f"Write denied to sensitive path: {resolved}"

    if for_write:
        for prefix in WRITE_DENIED_PREFIXES:
            if resolved.startswith(prefix):
                return f"Write denied to system directory: {prefix}"

    return None


def read_file(args: dict, **kwargs) -> str:
    """Read file contents with optional line range."""
    logger.debug("Beginning of read_file")
    file_path = args.get("file_path", "")
    offset = args.get("offset", 0)
    limit = args.get("limit")

    if not file_path:
        return json.dumps({"error": "No file_path provided"})

    path = Path(file_path).expanduser().resolve()
    # A restricted agent (kwargs carries its ceiling) may not read credentials.
    # Checked before existence so the error does not confirm which files exist.
    if kwargs.get("allowed_tools") is not None:
        reason = sensitive_read_reason(path)
        if reason:
            return json.dumps({"error": f"Read denied: {reason}"})
    if not path.exists():
        return json.dumps({"error": f"File not found: {file_path}"})
    if not path.is_file():
        return json.dumps({"error": f"Not a file: {file_path}"})

    try:
        content = path.read_text(errors="replace")
        lines = content.split("\n")

        if offset or limit:
            end = offset + limit if limit else len(lines)
            lines = lines[offset:end]

        result = "\n".join(lines)
        if len(result) > 100_000:
            result = result[:100_000] + "\n... [truncated]"

        return json.dumps({
            "content": result,
            "total_lines": len(content.split("\n")),
            "file_path": str(path),
        })
    except OSError as e:
        return json.dumps({"error": f"Read failed: {str(e)}"})


def write_file(args: dict, **kwargs) -> str:
    """Write/append to file."""
    logger.debug("Beginning of write_file")
    file_path = args.get("file_path", "")
    content = args.get("content", "")
    mode = args.get("mode", "overwrite")

    if not file_path:
        return json.dumps({"error": "No file_path provided"})

    path = Path(file_path).expanduser().resolve()

    # Security check
    denied = _is_path_denied(path)
    if denied:
        return json.dumps({"error": denied})

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with open(path, "a") as f:
                f.write(content)
        else:
            path.write_text(content)

        return json.dumps({
            "status": "success",
            "file_path": str(path),
            "bytes_written": len(content),
        })
    except OSError as e:
        return json.dumps({"error": f"Write failed: {str(e)}"})


def patch_file(args: dict, **kwargs) -> str:
    """Apply unified diff patch to file."""
    logger.debug("Beginning of patch_file")
    file_path = args.get("file_path", "")
    diff = args.get("diff", "")

    if not file_path or not diff:
        return json.dumps({"error": "file_path and diff are required"})

    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return json.dumps({"error": f"File not found: {file_path}"})

    try:
        # Apply patch using subprocess
        result = subprocess.run(
            ["patch", str(path)],
            input=diff,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return json.dumps({"status": "success", "output": result.stdout.strip()})
        else:
            # Fallback: try manual application
            return _apply_patch_manual(path, diff)
    except FileNotFoundError:
        # patch command not available, try manual
        return _apply_patch_manual(path, diff)


def _apply_patch_manual(path: Path, diff: str) -> str:
    """Manual patch application for simple unified diffs."""
    logger.debug("Beginning of _apply_patch_manual")
    try:
        content = path.read_text()
        lines = content.split("\n")

        # Parse diff hunks
        for match in re.finditer(r"@@ -(\d+),?\d* \+(\d+),?\d* @@", diff):
            pass  # Simple heuristic: find and replace

        # Simple search/replace approach for single-hunk patches
        old_lines = []
        new_lines = []
        in_hunk = False

        for line in diff.split("\n"):
            if line.startswith("@@"):
                in_hunk = True
                continue
            if in_hunk:
                if line.startswith("-"):
                    old_lines.append(line[1:])
                elif line.startswith("+"):
                    new_lines.append(line[1:])
                elif line.startswith(" "):
                    old_lines.append(line[1:])
                    new_lines.append(line[1:])

        if old_lines:
            old_text = "\n".join(old_lines)
            new_text = "\n".join(new_lines)
            if old_text in content:
                content = content.replace(old_text, new_text, 1)
                path.write_text(content)
                return json.dumps({"status": "success", "method": "manual"})

        return json.dumps({"error": "Could not apply patch — content mismatch"})
    except Exception as e:
        return json.dumps({"error": f"Patch failed: {str(e)}"})


def search_files(args: dict, **kwargs) -> str:
    """Regex search across files."""
    logger.debug("Beginning of search_files")
    pattern = args.get("pattern", "")
    search_path = args.get("path", ".")
    include = args.get("include")
    exclude = args.get("exclude")
    max_results = args.get("max_results", 50)

    if not pattern:
        return json.dumps({"error": "No pattern provided"})

    restricted = kwargs.get("allowed_tools") is not None
    if restricted:
        reason = sensitive_read_reason(Path(search_path))
        if reason:
            return json.dumps({"error": f"Search denied: {reason}"})

    try:
        cmd = ["grep", "-rn", "--include=*"]
        if include:
            cmd = ["grep", "-rn", f"--include={include}"]
        if exclude:
            cmd.append(f"--exclude={exclude}")
        # `-e` and `--`: the pattern and path are model-supplied, and a pattern
        # such as "-f /some/file" is otherwise parsed by grep as an option.
        cmd.extend(["-e", pattern, "--", search_path])

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )

        matches = [m for m in result.stdout.strip().split("\n") if m]
        if restricted:
            # grep has already walked the tree; drop hits from credential files
            # before anything reaches the model. Lines are `path:lineno:text`.
            def _allowed(line: str) -> bool:
                m = re.match(r"^(.*?):\d+:", line)
                return not (m and sensitive_read_reason(Path(m.group(1))))

            matches = [m for m in matches if _allowed(m)]
        matches = matches[:max_results]

        return json.dumps({
            "matches": matches,
            "count": len(matches),
            "pattern": pattern,
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Search timed out"})
    except Exception as e:
        return json.dumps({"error": f"Search failed: {str(e)}"})


# Register tools
registry.register(
    name="read_file",
    toolset="file",
    schema={
        "name": "read_file",
        "description": "Read file contents with optional line range.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file"},
                "offset": {"type": "integer", "description": "Start line (0-indexed)", "default": 0},
                "limit": {"type": "integer", "description": "Number of lines to read"},
            },
            "required": ["file_path"],
        },
    },
    handler=read_file,
    emoji="📖",
    read_only=True,
)

registry.register(
    name="write_file",
    toolset="file",
    schema={
        "name": "write_file",
        "description": "Write or append to a file. Creates parent directories if needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file"},
                "content": {"type": "string", "description": "Content to write"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
            },
            "required": ["file_path", "content"],
        },
    },
    handler=write_file,
    emoji="✏️",
)

registry.register(
    name="patch",
    toolset="file",
    schema={
        "name": "patch",
        "description": "Apply a unified diff patch to a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to file to patch"},
                "diff": {"type": "string", "description": "Unified diff content"},
            },
            "required": ["file_path", "diff"],
        },
    },
    handler=patch_file,
    emoji="🩹",
)

registry.register(
    name="search_files",
    toolset="file",
    schema={
        "name": "search_files",
        "description": "Search for a pattern across files using regex.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory to search in", "default": "."},
                "include": {"type": "string", "description": "File glob pattern to include"},
                "exclude": {"type": "string", "description": "File glob pattern to exclude"},
                "max_results": {"type": "integer", "description": "Max results", "default": 50},
            },
            "required": ["pattern"],
        },
    },
    handler=search_files,
    emoji="🔍",
    read_only=True,
)
