"""Values exchanged by a visual flow Activity and its executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ActivityContext:
    run_id: str
    node_path: str
    incoming_value: Any
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    flow_attributes: dict[str, Any]
    attributes: dict[str, Any]
    human_response: Any = None

    @property
    def previous_output(self) -> Any:
        return self.incoming_value

    @property
    def context(self) -> dict[str, Any]:
        return {"inputs": self.inputs, "outputs": self.outputs,
                "flow_attributes": self.flow_attributes}

    def attr(self, name: str) -> Any:
        if name in self.attributes:
            return self.attributes[name]
        if name in self.flow_attributes:
            return self.flow_attributes[name]
        raise KeyError(name)


@dataclass(frozen=True)
class ActivityResult:
    transient: str
    output: Any = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitResult:
    deadline: float | None = None
