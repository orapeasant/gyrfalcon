"""Typed run input.

Spec: §8 — "the single most reusable piece": a typed, schema-rendered,
timeout-bounded, resumable approval gate between an agent's proposal and an
irreversible action.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class RunInput(BaseModel):
    """A schema the server can render as a form."""

    model_config = ConfigDict(extra="allow")

    @classmethod
    def with_initial_data(cls, description: Optional[str] = None, **data: Any) -> "RunInput":
        """Seed a prompt with context (e.g. a preview of the action to approve)."""
        instance = cls.model_construct(**data)
        object.__setattr__(instance, "description", description)
        return instance

    @classmethod
    def schema_for_render(cls) -> dict[str, Any]:
        return cls.model_json_schema()
