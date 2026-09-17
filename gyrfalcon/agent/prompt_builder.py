"""System prompt assembly — stateless functions."""

import functools
import os
import platform
import datetime
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home, get_skills_dir, get_app_name
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("prompt_builder")



DEFAULT_AGENT_IDENTITY_TEMPLATE = """You are {app_name}, a powerful AI assistant. You are a tool-calling agent that helps users accomplish tasks by breaking them down into steps and using available tools.

Key behaviors:
- Use tools to accomplish tasks rather than just providing information
- Be proactive about finding solutions
- Ask for clarification when the task is ambiguous
- Track progress on complex tasks
- Remember context from earlier in the conversation
- Use `memory_recall` and `memory_store` to persist and retrieve important user preferences and facts across sessions

## Conversation History & Context Retrieval

When the user references past interactions — such as asking about previous questions, prior discussions, earlier context, what they worked on, or anything that implies historical conversation data — you MUST use the `session_search` tool to retrieve relevant information from the local session database before responding. Examples:
- "what was my first question" → use session_search with action=list_recent, role_filter=user
- "what did I ask about SAP" → use session_search with action=search, query="SAP"
- "continue where we left off" → use session_search with action=list_recent to find recent context
- "remind me what you said about X" → use session_search with action=search, query="X", role_filter=assistant

Never say you don't have access to past conversations. The session database stores all prior chat history locally.

## Handling Tool Failures

When a tool call fails, report what actually happened. Do not turn a failure into a claim that you lack the capability.

- Show the real error text from the tool, not a paraphrase of it. The exact message is usually what tells the user how to fix it.
- Say which command or tool produced it, so the user can reproduce it themselves.
- If the fix requires something you cannot do — an interactive login, a browser prompt, a credential only the user holds, elevated permissions — give them the exact command to run. That workaround is a useful answer, not a fallback.
- Distinguish "this failed for reason X" from "I am unable to do this". Auth errors, expired tokens, missing config, and permission denials are all results worth reporting, not reasons to refuse.
- Never silently swallow a failed tool call or present a partial result as if it succeeded.
"""


def build_environment_hints() -> str:
    """OS/shell/cwd/date detection. Returns formatted environment block."""
    logger.debug("Beginning of build_environment_hints")
    parts = []
    parts.append(f"OS: {platform.system()} {platform.release()}")
    parts.append(f"Architecture: {platform.machine()}")
    parts.append(f"Python: {platform.python_version()}")

    shell = os.environ.get("SHELL", "unknown")
    parts.append(f"Shell: {shell}")

    cwd = os.getcwd()
    parts.append(f"Working Directory: {cwd}")
    parts.append(f"Gyrfalcon Home: {get_gyrfalcon_home()}")

    now = datetime.datetime.now()
    parts.append(f"Date: {now.strftime('%Y-%m-%d %H:%M %Z')}")

    user = os.environ.get("USER", os.environ.get("USERNAME", "unknown"))
    parts.append(f"User: {user}")

    return "\n".join(parts)


def build_workspace_policy() -> str:
    """Strict rule: never write into the codebase you're reading.

    `Working Directory` above is whatever project or codebase the user
    pointed the session at — reading it is the whole point of being there.
    This is the rule that stops the agent from also treating it as scratch
    space for its own output.
    """
    logger.debug("Beginning of build_workspace_policy")
    home = get_gyrfalcon_home()
    return (
        "## Workspace Boundaries\n\n"
        "**Never create, edit, delete, or otherwise modify files inside the codebase "
        "or project folder you are working in (the `Working Directory` above, or any "
        "path a user or tool points you at outside Gyrfalcon Home). Reading files "
        "there is always fine and expected — this rule is about writes only.**\n\n"
        f"Any file *you* produce as part of doing a task — generated code, scripts, "
        f"downloaded content, reports, notes, working files of any kind — belongs "
        f"under Gyrfalcon Home instead: `{home}` (the `GYRFALCON_HOME` environment "
        f"variable, defaulting to `~/.gyrfalcon`). Never write it into the codebase, "
        f"even temporarily, even if the user's request is *about* that codebase.\n\n"
        "Group what you write under Gyrfalcon Home into subfolders by purpose rather "
        "than dropping everything flat in its root — for example a subfolder per task, "
        "topic, or output type (scripts, reports, downloads, ...), the same way "
        "Gyrfalcon's own subsystems already keep `skills/`, `flows/`, and `sandboxes/` "
        "separate. Create a subfolder before writing into it if one doesn't exist yet.\n\n"
        "This rule is strict and has no task-specific exception: even a request that is "
        "explicitly about editing the codebase does not authorize writing into it — "
        "produce the change (patch, file, script) under Gyrfalcon Home instead and tell "
        "the user where it is, rather than applying it to the codebase directly."
    )


def build_skills_system_prompt(
    skills_dir: Path | None = None,
    enabled_tools: set[str] | None = None,
    enabled_toolsets: list[str] | None = None,
    platform_name: str | None = None,
    preloaded_skills: list[str] | None = None,
) -> str:
    """Skills index for system prompt."""
    logger.debug("Beginning of build_skills_system_prompt")
    if skills_dir is None:
        skills_dir = get_skills_dir()

    if not skills_dir.exists():
        return ""

    skills = []
    for skill_path in sorted(skills_dir.iterdir()):
        if skill_path.is_file() and skill_path.suffix == ".md":
            name = skill_path.stem
            # Read first few lines for description
            try:
                content = skill_path.read_text()
                lines = content.split("\n")
                desc = ""
                for line in lines[5:15]:
                    if line.strip() and not line.startswith("#") and not line.startswith("---"):
                        desc = line.strip()
                        break
                skills.append(f"- {name}: {desc}")
            except OSError:
                skills.append(f"- {name}")
        elif skill_path.is_dir() and (skill_path / "SKILL.md").exists():
            name = skill_path.name
            skills.append(f"- {name}")

    if not skills:
        return ""

    header = "## Available Skills\n\nUse `skills_list` to see details. Use `skill_view` to load a skill.\n\n"
    return header + "\n".join(skills)


def build_context_files_prompt(cwd: str | None = None, skip_soul: bool = False) -> str:
    """Loads SOUL.md, GYRFALCON.md, AGENTS.md from cwd up to git root."""
    logger.debug("Beginning of build_context_files_prompt")
    if cwd is None:
        cwd = os.getcwd()

    context_parts = []

    # Load SOUL.md (global personality)
    if not skip_soul:
        soul = load_soul_md()
        if soul:
            context_parts.append(f"## User Profile\n\n{soul}")

    # Search up for context files
    search_dir = Path(cwd)
    checked = set()
    context_files = ["GYRFALCON.md", "AGENTS.md", ".cursorrules"]

    while search_dir != search_dir.parent:
        if str(search_dir) in checked:
            break
        checked.add(str(search_dir))

        for fname in context_files:
            fpath = search_dir / fname
            if fpath.exists():
                try:
                    content = fpath.read_text(errors="replace")[:10000]
                    context_parts.append(f"## Context ({fname})\n\n{content}")
                except OSError:
                    pass

        # Stop at git root
        if (search_dir / ".git").exists():
            break
        search_dir = search_dir.parent

    return "\n\n".join(context_parts)


def load_soul_md() -> Optional[str]:
    """Global profile from ~/.gyrfalcon/SOUL.md."""
    logger.debug("Beginning of load_soul_md")
    soul_path = get_gyrfalcon_home() / "SOUL.md"
    if soul_path.exists():
        try:
            return soul_path.read_text(errors="replace")[:5000]
        except OSError:
            pass
    return None


# Capabilities a user asks for by name, where the tool schema alone does not make
# the connection obvious. Keyed by tool name; only rendered when that tool is enabled.
# CLIs worth naming explicitly when installed. The model reads "shell access" as
# generic and concludes it has no way to reach a specific service, so the ones
# actually present on this machine are listed by name.
_NOTABLE_CLIS: tuple[str, ...] = (
    "aws", "az", "gcloud", "kubectl", "docker", "gh", "git",
    "terraform", "psql", "mysql", "sqlite3", "curl", "jq",
)


@functools.lru_cache(maxsize=1)
def detect_available_clis() -> tuple[str, ...]:
    """Which notable CLIs are on PATH. Cached — PATH does not change mid-process."""
    import shutil
    return tuple(name for name in _NOTABLE_CLIS if shutil.which(name))


def _terminal_capability_note() -> str | None:
    """Tell the agent which real CLIs the shell can reach.

    Grounded in what is actually installed so the agent neither refuses work it
    can do nor claims a CLI this machine does not have.
    """
    clis = detect_available_clis()
    if not clis:
        return None
    return (
        "### Shell and command-line tools\n"
        "The `terminal` tool runs real shell commands on this machine, so any installed "
        "CLI is available to you. Detected on PATH: "
        + ", ".join(f"`{c}`" for c in clis) + ".\n"
        "- Never say you lack a way to reach a service before checking whether its CLI "
        "is available here — e.g. query AWS with `aws ...`, Azure with `az ...`.\n"
        "- Authentication uses whatever credentials are already configured on this "
        "machine. If a command fails on auth or permissions, report the actual error "
        "rather than concluding you have no access."
    )


_CAPABILITY_NOTES: dict[str, Any] = {
    "scheduler": (
        "### Scheduling\n"
        "You can create and manage scheduled jobs yourself with the `scheduler` tool — "
        "do not tell the user you are unable to schedule things.\n"
        "- Schedules accept a cron expression (`0 9 * * 1-5`), a duration (`30m`, `every 2h`), "
        "an ISO timestamp, or natural language (`every morning at 9am`).\n"
        "- A job runs an agent prompt or a skill; it cannot run a raw shell command.\n"
        "- Jobs only fire while the gateway is running. The tool reports this back — "
        "if `will_fire` is false, say so and tell the user to start it with `gyrfalcon gateway`."
    ),
    "terminal": _terminal_capability_note,
}


def build_capabilities_prompt(enabled_tools: set[str] | None = None) -> str:
    """Describe enabled tools whose purpose isn't obvious from the schema alone.

    Without this the model sees the tool but never connects it to what the user
    asked for, and answers that it cannot do something it can.
    """
    logger.debug("Beginning of build_capabilities_prompt")
    if enabled_tools is None:
        from gyrfalcon.toolsets import _GYRFALCON_CORE_TOOLS
        enabled_tools = set(_GYRFALCON_CORE_TOOLS)

    notes = []
    for tool, note in _CAPABILITY_NOTES.items():
        if tool not in enabled_tools:
            continue
        text = note() if callable(note) else note
        if text:
            notes.append(text)
    if not notes:
        return ""
    return "## Capabilities\n\n" + "\n\n".join(notes)


def build_system_prompt(
    skills_dir: Path | None = None,
    enabled_tools: set[str] | None = None,
    enabled_toolsets: list[str] | None = None,
    platform_name: str | None = None,
    preloaded_skills: list[str] | None = None,
    skip_context_files: bool = False,
    skip_memory: bool = False,
    memory_guidance: str | None = None,
) -> str:
    """Assemble the full system prompt."""
    logger.debug("Beginning of build_system_prompt")
    parts = []

    # 1. Agent identity
    parts.append(DEFAULT_AGENT_IDENTITY_TEMPLATE.format(app_name=get_app_name()))

    # 2. Environment hints
    parts.append(f"## Environment\n\n{build_environment_hints()}")

    # 2b. Workspace boundaries — strict, always included
    parts.append(build_workspace_policy())

    # 3. Memory guidance
    if not skip_memory and memory_guidance:
        parts.append(f"## Memory\n\n{memory_guidance}")

    # 4. Capabilities that need framing beyond their tool schema
    capabilities = build_capabilities_prompt(enabled_tools)
    if capabilities:
        parts.append(capabilities)

    # 5. Skills index
    skills_prompt = build_skills_system_prompt(
        skills_dir, enabled_tools, enabled_toolsets, platform_name, preloaded_skills
    )
    if skills_prompt:
        parts.append(skills_prompt)

    # 5. Context files
    if not skip_context_files:
        ctx = build_context_files_prompt()
        if ctx:
            parts.append(ctx)

    return "\n\n".join(parts)
