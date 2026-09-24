"""Platform adapters.

`base` holds the contract. The registry maps a platform name to the adapter that
implements it as a `module:Class` string, imported lazily — so a platform whose
SDK is not installed costs a clear message when it is *enabled*, not an
ImportError at gateway startup for everyone.
"""

from __future__ import annotations

import importlib
from typing import Type

from gyrfalcon.gateway.platforms.base import (  # noqa: F401  (re-exported)
    DONE,
    FAILED,
    QUEUED,
    WORKING,
    ApprovalInteraction,
    BasePlatformAdapter,
    MessageEvent,
    SendResult,
    SessionSource,
)

#: platform name -> "module:Class"
ADAPTER_REGISTRY: dict[str, str] = {
    "slack": "gyrfalcon.gateway.platforms.slack:SlackAdapter",
    "teams": "gyrfalcon.gateway.platforms.teams:TeamsAdapter",
}

#: platform name -> what to install if its adapter's dependencies are missing.
INSTALL_HINTS: dict[str, str] = {
    "slack": "uv sync --extra slack",
    "teams": "uv sync --extra teams",
}


class AdapterUnavailable(RuntimeError):
    """The platform is unknown, or its adapter could not be imported."""


def load_adapter_class(name: str) -> Type[BasePlatformAdapter]:
    target = ADAPTER_REGISTRY.get(name)
    if not target:
        raise AdapterUnavailable(
            f"No adapter for platform '{name}'. Known platforms: {', '.join(sorted(ADAPTER_REGISTRY)) or 'none'}."
        )
    module_name, _, class_name = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        hint = INSTALL_HINTS.get(name)
        raise AdapterUnavailable(
            f"Platform '{name}' is enabled but its adapter could not be imported ({exc})."
            + (f" Install its dependencies with: {hint}" if hint else "")
        ) from exc
    return getattr(module, class_name)
