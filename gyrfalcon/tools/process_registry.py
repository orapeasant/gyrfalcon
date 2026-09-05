"""Background process registry — tracks background terminal processes."""

from __future__ import annotations

import threading
from typing import Optional
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("process_registry")



class ProcessEntry:
    def __init__(self, pid: int, command: str, task_id: str | None = None):
        self.pid = pid
        self.command = command
        self.task_id = task_id
        self.completed = False
        self.exit_code: Optional[int] = None
        self.output: str = ""


class ProcessRegistry:
    """Tracks background terminal processes for notification on completion."""

    def __init__(self):
        self._processes: dict[int, ProcessEntry] = {}
        self._lock = threading.Lock()

    def register(self, pid: int, command: str, task_id: str | None = None) -> None:
        logger.debug("Beginning of register")
        with self._lock:
            self._processes[pid] = ProcessEntry(pid, command, task_id)

    def mark_completed(self, pid: int, exit_code: int, output: str = "") -> None:
        logger.debug("Beginning of mark_completed")
        with self._lock:
            if pid in self._processes:
                self._processes[pid].completed = True
                self._processes[pid].exit_code = exit_code
                self._processes[pid].output = output

    def get(self, pid: int) -> Optional[ProcessEntry]:
        logger.debug("Beginning of get")
        return self._processes.get(pid)

    def get_completed(self) -> list[ProcessEntry]:
        logger.debug("Beginning of get_completed")
        with self._lock:
            completed = [p for p in self._processes.values() if p.completed]
            # Remove completed entries
            for p in completed:
                del self._processes[p.pid]
            return completed

    def list_running(self) -> list[ProcessEntry]:
        logger.debug("Beginning of list_running")
        with self._lock:
            return [p for p in self._processes.values() if not p.completed]


# Global instance
process_registry = ProcessRegistry()
