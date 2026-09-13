"""Result records.

Spec: §6.1 — `State.data` holds either an inline value or a pointer, so small
results travel inline and large ones are persisted and referenced without a
separate "is it inline?" flag.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

#: Process-local backing store. A real deployment swaps this for a storage block.
_STORE: dict[str, Any] = {}


class ResultRecordMetadata(BaseModel):
    """A pointer to a persisted result."""

    model_config = ConfigDict(extra="allow")

    storage_key: str
    serializer: str = "json"
    expiration: Optional[float] = None


class ResultRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    result: Any
    metadata: ResultRecordMetadata


class ResultStore:
    """Couples a backing store with the inline/pointer decision."""

    def __init__(self, backing: Optional[dict[str, Any]] = None, threshold: int = 1024):
        self._backing = backing if backing is not None else _STORE
        self.threshold = threshold

    def persist(self, key: str, value: Any) -> ResultRecordMetadata:
        self._backing[key] = value
        return ResultRecordMetadata(storage_key=key)

    def read(self, meta: ResultRecordMetadata) -> Any:
        return self._backing.get(meta.storage_key)

    def should_persist(self, value: Any) -> bool:
        try:
            return len(repr(value)) > self.threshold
        except Exception:
            return False


def resolve_result(meta: ResultRecordMetadata) -> Any:
    return _STORE.get(meta.storage_key)
