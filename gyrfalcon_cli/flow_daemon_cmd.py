"""CLI lifecycle for the visual flow worker daemon."""

from __future__ import annotations

import signal
import threading


def run_flow_daemon(args: list[str]) -> None:
    action = args[0] if args else "run"
    if action != "run":
        raise SystemExit("Usage: gyrfalcon flow-daemon run")
    from gyrfalcon.db import dispose_database_pools, open_database
    from gyrfalcon.db.migrations import ensure_schema
    database = open_database()
    try:
        ensure_schema(database)
    finally:
        database.close()

    from gyrfalcon.flow.visual_daemon import VisualFlowDaemon
    daemon = VisualFlowDaemon()
    daemon.start()
    stopped = threading.Event()

    def stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        print("Visual flow daemon is running. Press Ctrl+C to stop.")
        stopped.wait()
    finally:
        daemon.stop()
        dispose_database_pools()
