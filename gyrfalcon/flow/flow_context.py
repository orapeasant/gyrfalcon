"""Initialization and provider hooks for a visual run's durable business context."""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any, Callable

ContextProvider = Callable[[dict[str, Any]], dict[str, Any] | None]
_LOCK = threading.Lock()
_PROVIDERS: dict[str, list[ContextProvider]] = {"initiator": [], "business": []}


def flow_context_provider(*, source: str = "business"):
    """Register a worker-side fallback loader for initiator or business context.

    The provider receives ``store`` plus JSON-safe ``run``, ``inputs``, and
    ``initiator`` data. Return a mapping; providers in each source category
    fill missing keys in registration order. This is where app-specific context
    tables and business tables can be queried without hard-coding their schema
    in the flow engine.
    """
    if source not in _PROVIDERS:
        raise ValueError("Flow context provider source must be 'initiator' or 'business'")

    def register(fn: ContextProvider) -> ContextProvider:
        with _LOCK:
            _PROVIDERS[source].append(fn)
        return fn
    return register


def _providers(source: str) -> list[ContextProvider]:
    with _LOCK:
        return list(_PROVIDERS[source])


def initialize_flow_context(store, run: dict[str, Any], principal) -> dict[str, Any]:
    """Load or build a run context once; later worker attempts reuse its row."""
    existing = store.get_flow_context(run["id"])
    if existing is not None:
        return existing

    inputs = deepcopy(run.get("parameters") or {})
    if not isinstance(inputs, dict):
        raise ValueError("Flow inputs must be an object")
    initiator = _identity_context(store, principal)
    provider_data = {"store": store, "run": _safe_run(run), "inputs": deepcopy(inputs),
                     "initiator": deepcopy(initiator)}
    initiator_extras: dict[str, Any] = {}
    for provider in _providers("initiator"):
        values = provider(provider_data)
        if values is not None:
            if not isinstance(values, dict):
                raise ValueError("Initiator context providers must return an object")
            for key, value in values.items():
                initiator_extras.setdefault(key, value)
    business: dict[str, Any] = {}
    provider_data["initiator_context"] = {**initiator, **initiator_extras}
    for provider in _providers("business"):
        values = provider(provider_data)
        if values is not None:
            if not isinstance(values, dict):
                raise ValueError("Business context providers must return an object")
            for key, value in values.items():
                business.setdefault(key, value)

    built = {"inputs": inputs, "initiator": {**initiator, **initiator_extras},
             "business": business}
    # Concurrent duplicate dispatchers must agree on one captured snapshot.
    return store.save_flow_context(run["id"], built, only_if_absent=True)


def _safe_run(run: dict[str, Any]) -> dict[str, Any]:
    return {key: run.get(key) for key in (
        "id", "tenant_id", "environment_id", "user_id", "definition_id",
        "version", "deployment_id", "trigger_kind", "trigger_ref")}


def _identity_context(store, principal) -> dict[str, Any]:
    """Resolve stable initiator and organization facts from auth database rows."""
    from sqlalchemy import and_, select
    from gyrfalcon.db.schema import AUTH_MEMBERSHIP_ATTRIBUTES, AUTH_ORGS, AUTH_USERS

    user = org = attrs = None
    with store.db.connect() as conn:
        user = conn.connection.execute(select(
            AUTH_USERS.c.id, AUTH_USERS.c.email, AUTH_USERS.c.display_name,
            AUTH_USERS.c.kind).where(AUTH_USERS.c.id == principal.user_id)).mappings().first()
        org = conn.connection.execute(select(
            AUTH_ORGS.c.id, AUTH_ORGS.c.name).where(
                AUTH_ORGS.c.id == principal.tenant_id)).mappings().first()
        attrs = conn.connection.execute(select(
            AUTH_MEMBERSHIP_ATTRIBUTES.c.department,
            AUTH_MEMBERSHIP_ATTRIBUTES.c.business_unit).where(and_(
                AUTH_MEMBERSHIP_ATTRIBUTES.c.tenant_id == principal.tenant_id,
                AUTH_MEMBERSHIP_ATTRIBUTES.c.user_id == principal.user_id))).mappings().first()
    return {
        "user": dict(user) if user else {"id": principal.user_id,
            "display_name": principal.display_name, "email": principal.email},
        "organization": dict(org) if org else {"id": principal.tenant_id},
        "membership": dict(attrs) if attrs else {},
        "roles": sorted(principal.roles),
        "source": principal.source,
    }
