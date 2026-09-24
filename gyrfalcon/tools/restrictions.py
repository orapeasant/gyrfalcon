"""Tool ceilings — what a restricted agent may hand on, and what it may read.

A *restricted* agent (`AIAgent(restrict_tools=True)`, which is what the gateway
builds for every chat platform) is one whose tool set is a security boundary
rather than a convenience. Filtering the schema the model is shown is not
enough to make it one: the model can name a tool it was never offered, and any
tool that starts more work — a child agent, a scheduled job — can quietly hand
that work a wider set than its caller holds.

So restriction has three parts, and this module owns the last two:

1. dispatch refuses any tool outside the offered set (`model_tools.py`);
2. tools that start work clamp what they start to the caller's set
   (`clamp_toolsets`);
3. read tools refuse credential material (`sensitive_read_reason`).

Nothing here applies to an unrestricted agent — the CLI behaves exactly as it
did — because every entry point takes `allowed_tools=None` to mean "no ceiling".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from gyrfalcon.toolsets import resolve_multiple_toolsets

#: A toolset name that resolves to no real tool. `AIAgent(enabled_toolsets=[])`
#: is falsy and falls back to the *default core set* — shell included — so an
#: empty clamp must not be returned as an empty list. This names nothing, the
#: registry ignores unknown names, and the agent ends up with no tools at all.
NO_TOOLS = "__no_tools__"


def clamp_toolsets(requested: Optional[Iterable[str]], allowed: Optional[frozenset[str]]) -> Optional[list[str]]:
    """The toolsets a child (agent or job) may be given under a ceiling.

    `allowed` None means the caller is unrestricted and `requested` passes
    through untouched (including None, meaning "the default set"). Otherwise the
    result is always a concrete list of *tool names* — never None, because
    None means "the default set", which is exactly the widening to prevent —
    and never contains anything outside `allowed`. Asking for more than the
    ceiling silently yields less, not an error: the child simply cannot have it.
    When nothing survives the clamp the result is `[NO_TOOLS]`, never `[]`.
    """
    if allowed is None:
        return list(requested) if requested is not None else None
    if not requested:
        return sorted(allowed) or [NO_TOOLS]
    return sorted(resolve_multiple_toolsets(list(requested)) & allowed) or [NO_TOOLS]


# Names that are credentials wherever they live.
_SECRET_FILE_NAMES = (".env", ".netrc", ".npmrc", ".pypirc", "credentials", "id_rsa", "id_ed25519", "id_ecdsa")

# Directories under the user's home holding credentials for other systems.
_SECRET_HOME_DIRS = (".ssh", ".aws", ".gnupg", ".kube", ".config/gcloud", ".config/gh", ".docker")

_SECRET_ABSOLUTE = ("/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/ssh")


def _home_dirs() -> list[Path]:
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    return [get_gyrfalcon_home().resolve()]


def sensitive_read_reason(path: Path) -> Optional[str]:
    """Why a restricted agent may not read `path`, or None if it may.

    A denylist, and a best-effort one — it stops the obvious exfiltration (ask
    the bot to paste `secrets.json`, `.env`, an SSH key) rather than containing
    a determined reader. Real containment of file access needs a working-
    directory jail, which is a separate piece of work; this exists so the v1
    toolset is not a one-message credential leak.

    The whole of `GYRFALCON_HOME` is denied, not just named files in it:
    `config.yaml` carries the OIDC client secret and `auth.json`, `secrets.json`
    and the dashboard token sit beside it, and a list of "the sensitive ones"
    goes stale the first time a store is added.
    """
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return "unresolvable path"

    for home in _home_dirs():
        if resolved == home or home in resolved.parents:
            return "Gyrfalcon's own state directory holds credentials"

    s = str(resolved)
    for absolute in _SECRET_ABSOLUTE:
        if s == absolute or s.startswith(absolute + "/"):
            return "system credential file"

    user_home = Path(os.path.expanduser("~")).resolve()
    for rel in _SECRET_HOME_DIRS:
        d = user_home / rel
        if resolved == d or d in resolved.parents:
            return "credential directory"

    name = resolved.name
    if name in _SECRET_FILE_NAMES or name.startswith(".env."):
        return "credential file"

    return None
