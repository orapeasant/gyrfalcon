"""Transactions and compensating actions.

Spec: §6.3.

`RolledBack` is a COMPLETED-type terminal state: the run *did* succeed, its
effects were undone. The compensating-action pattern (`on_rollback`) is where
"the agent sent an email, then the flow failed, so send a correction" belongs.
"""

from __future__ import annotations

import contextlib
import contextvars
import enum
import logging
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("gyrfalcon.flow.transactions")


class TransactionState(str, enum.Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    STAGED = "STAGED"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"


class IsolationLevel(str, enum.Enum):
    READ_COMMITTED = "READ_COMMITTED"
    SERIALIZABLE = "SERIALIZABLE"


class Transaction:
    """A scoped k/v store plus a rollback stack. Transactions nest."""

    def __init__(self, parent: Optional["Transaction"] = None,
                 isolation_level: IsolationLevel = IsolationLevel.READ_COMMITTED):
        self.parent = parent
        self.isolation_level = isolation_level
        self.state = TransactionState.PENDING
        self._store: dict[str, Any] = {}
        self._rollbacks: list[tuple[Callable, Any]] = []
        self._children: list["Transaction"] = []

    # -- scoped store --------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        self._store[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        """Child lookups fall through to the parent (§6.3)."""
        if key in self._store:
            return self._store[key]
        if self.parent is not None:
            return self.parent.get(key, default)
        return default

    # -- rollback registration ----------------------------------------------
    def stage_rollback(self, hook: Callable, template: Any = None) -> None:
        self._rollbacks.append((hook, template))

    def commit(self) -> None:
        for child in self._children:
            if child.state is TransactionState.STAGED:
                child.commit()
        self.state = TransactionState.COMMITTED

    def rollback(self) -> None:
        """Compensate in reverse order — most recent effect undone first."""
        for child in reversed(self._children):
            if child.state not in (TransactionState.ROLLED_BACK,):
                child.rollback()
        for hook, _template in reversed(self._rollbacks):
            try:
                hook(self)
            except Exception:
                logger.warning("rollback hook %r raised; continuing", hook, exc_info=True)
        self.state = TransactionState.ROLLED_BACK


_TXN: contextvars.ContextVar[Optional[Transaction]] = contextvars.ContextVar(
    "gyrfalcon_transaction", default=None
)


def get_transaction() -> Optional[Transaction]:
    return _TXN.get()


@contextlib.contextmanager
def transaction(
    isolation_level: IsolationLevel = IsolationLevel.READ_COMMITTED,
) -> Iterator[Transaction]:
    parent = _TXN.get()
    txn = Transaction(parent=parent, isolation_level=isolation_level)
    if parent is not None:
        parent._children.append(txn)

    token = _TXN.set(txn)
    txn.state = TransactionState.ACTIVE
    try:
        yield txn
    except BaseException:
        txn.rollback()
        raise
    else:
        txn.state = TransactionState.STAGED
        if parent is None:
            txn.commit()
    finally:
        _TXN.reset(token)
