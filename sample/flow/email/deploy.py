"""Create the 5-minute deployment for `poll_mailbox`.

    uv run python sample/flow/email/deploy.py

There is no flow CLI yet, so a deployment is created by calling the store
directly. Idempotent: re-running updates the existing deployment's schedule and
parameters rather than creating a second one that polls the same mailbox twice.

A deployment is only half of it — the `Runner` is what ticks and starts due
runs. It runs inside the gateway (`uv run gyrfalcon gateway`); to try this
standalone see `run_standalone()` at the bottom.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The flow must be imported before the deployment can name it: DeploymentStore
# .create() validates against the registry, precisely so a deployment for an
# unimported flow can't sit there ticking forever finding nothing to start.
sys.path.insert(0, str(Path(__file__).parent))
import mailbox_flow  # noqa: E402,F401  (importing IS the registration)

from gyrfalcon.flow.deployments import get_deployment_store  # noqa: E402

DEPLOYMENT_NAME = "mailbox-poll"


def main() -> None:
    store = get_deployment_store()

    binding = dict(
        # parse_schedule also takes "5m", "*/5 * * * *", or "every 5 minutes".
        schedule="every 5m",
        parameters={"folder": "INBOX", "limit": 50},
        tags=["email"],
        # One poll at a time. Without this, a tick that overruns 5 minutes
        # would start a second poll against the same UID high-water mark.
        concurrency_limit=1,
    )

    existing = store.get_by_name(DEPLOYMENT_NAME)
    if existing:
        # `update` rewrites every field it is given, so pass the whole binding
        # — omitting tags would blank them rather than leave them alone.
        dep = store.update(existing["id"], name=DEPLOYMENT_NAME, **binding)
        action = "Updated"
    else:
        dep = store.create(name=DEPLOYMENT_NAME, flow_name="poll_mailbox", **binding)
        action = "Created"

    print(f"{action} deployment {dep['name']!r} ({dep['id']})")
    print(f"  flow:       {dep['flow_name']}")
    print(f"  schedule:   {dep['schedule_raw']}  -> {dep['schedule']}")
    print(f"  parameters: {dep['parameters']}")
    print(f"  next run:   {dep['next_run_at']}")
    print()
    print("The runner starts due runs; it ticks inside `uv run gyrfalcon gateway`.")
    print("To watch it here instead, run: python sample/flow/email/deploy.py --run")


def run_standalone(minutes: float = 11.0) -> None:
    """Tick the runner in this process, for trying the deployment out.

    The runner is normally owned by the gateway — this is a way to see the
    5-minute schedule actually fire without starting one.
    """
    import time

    from gyrfalcon.flow.runner import start_runner, stop_runner

    runner = start_runner(tick_seconds=5.0)
    print(f"Runner started; watching for ~{minutes:.0f} minutes. Ctrl+C to stop.")
    try:
        time.sleep(minutes * 60)
    except KeyboardInterrupt:
        pass
    finally:
        stop_runner()
        print("Runner stopped.")
    _ = runner


if __name__ == "__main__":
    main()
    if "--run" in sys.argv:
        run_standalone()
