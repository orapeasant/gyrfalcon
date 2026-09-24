"""Gateway — multi-platform message routing framework.

Kept import-light on purpose: `GatewayRunner` pulls in the agent, the scheduler
and their singletons, and a test (or a CLI command that only wants the config)
should not pay for — or be affected by — any of that. The names below resolve
lazily on first access.
"""

from __future__ import annotations

from typing import Any

__all__ = ["GatewayRunner", "AgentCache", "GatewayConfig"]


def __getattr__(name: str) -> Any:
    if name in ("GatewayRunner", "AgentCache"):
        from gyrfalcon.gateway import run

        return getattr(run, name)
    if name == "GatewayConfig":
        from gyrfalcon.gateway.config import GatewayConfig

        return GatewayConfig
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
