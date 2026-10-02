"""Programmatic entry points for visual flow invocations."""

from __future__ import annotations

from typing import Any


def start_flow(definition_id: str, inputs: dict[str, Any] | None = None, *,
               version: int | None = None, caller_key: str | None = None) -> str:
    """Queue a published flow and return its run ID immediately."""
    from gyrfalcon.flow.graphs import GraphStore

    store = GraphStore()
    try:
        selected = version or store.latest_version(definition_id)
        if selected is None:
            raise ValueError("Publish the flow before starting it")
        return store.run(definition_id, selected, inputs or {},
                         caller_key=caller_key, trigger="python")["run_id"]
    finally:
        store.close()


def start_deployment(deployment_id: str | None = None, *, short_name: str | None = None,
                     inputs: dict[str, Any] | None = None,
                     caller_key: str | None = None) -> str:
    """Queue a deployment using its pinned version and return its run ID."""
    if (deployment_id is None) == (short_name is None):
        raise ValueError("provide exactly one deployment_id or short_name")
    from gyrfalcon.flow.runtime_store import FlowRuntimeStore
    from gyrfalcon.identity import require_principal

    principal = require_principal()
    store = FlowRuntimeStore(tenant_id=principal.tenant_id)
    try:
        deployment = store.get_deployment(deployment_id, short_name=short_name) \
            if deployment_id is not None else store.get_deployment(short_name=short_name)
        if deployment is None or deployment.get("paused"):
            raise KeyError(deployment_id or short_name)
        values = {**(deployment.get("parameters") or {}), **(inputs or {})}
        run = store.create_run(
            deployment["definition_id"], deployment["version"], values, "python",
            user_id=principal.user_id, deployment_id=deployment["id"],
            caller_key=caller_key,
            trigger_ref=short_name or deployment["id"],
        )
        store.record_event(run["id"], "flow.queued", {"trigger": "python"})
        return run["id"]
    finally:
        store.close()
