"""Scheduler tool — unified scheduled job management."""

from __future__ import annotations

import json

from gyrfalcon.tools import registry
from gyrfalcon.scheduler import job_store
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.tools.restrictions import NO_TOOLS

logger = get_logger("tools.scheduler")


def _scheduler_runtime_state() -> dict:
    """Whether scheduled jobs will actually fire right now.

    The scheduler ticks inside the gateway process only, so a job can be created
    successfully and still never run. Callers surface this so the agent can say so
    instead of reporting a bare success.
    """
    from gyrfalcon.config import cfg_get

    from gyrfalcon.scheduler import is_scheduler_running

    enabled = bool(cfg_get("scheduler.enabled", True))
    running = is_scheduler_running()

    state = {"scheduler_enabled": enabled, "scheduler_running": running, "will_fire": enabled and running}
    if not enabled:
        state["note"] = "Scheduler is disabled in config (scheduler.enabled=false); jobs will not run."
    elif not running:
        state["note"] = (
            "Jobs are saved but will not fire: the scheduler only ticks while the gateway "
            "is running. Start it with `gyrfalcon gateway`."
        )
    return state


def scheduler_tool(args: dict, **kwargs) -> str:
    """Unified scheduled job management: create/list/update/pause/resume/remove/trigger."""
    logger.debug("Beginning of scheduler_tool")
    action = args.get("action", "list")

    # A restricted caller (a chat-platform agent) starts jobs no wider than
    # itself, and may only rewrite or delete jobs that carry its own
    # restriction. Otherwise a job created with the default toolset — which
    # includes the shell — is a one-step escalation, and editing an existing
    # unrestricted job's prompt hands that job's privileges to whoever
    # controls the text.
    allowed = kwargs.get("allowed_tools")
    if allowed is not None and action in ("update", "remove"):
        existing = job_store.get(args.get("job_id", ""))
        if existing and not existing.get("restrict_tools"):
            return json.dumps({
                "error": f"Job {args.get('job_id')} was not created from a restricted session; "
                         f"it cannot be {'edited' if action == 'update' else 'removed'} from here."
            })

    if action == "list":
        jobs = job_store.list_all()
        return json.dumps({"jobs": jobs, **_scheduler_runtime_state()})

    elif action == "status":
        return json.dumps({"job_count": len(job_store.list_all()), **_scheduler_runtime_state()})

    elif action == "create":
        prompt = args.get("prompt", "")
        schedule = args.get("schedule", "")
        name = args.get("name", "")

        if not prompt or not schedule:
            return json.dumps({"error": "prompt and schedule are required"})

        job = {
            "prompt": prompt,
            "schedule": schedule,
            "name": name,
            "repeat": args.get("repeat", True),
            "model": args.get("model"),
            "script": args.get("script"),
            "no_agent": args.get("no_agent", False),
            "skills": args.get("skills", []),
        }
        if allowed is not None:
            job["enabled_toolsets"] = sorted(allowed) or [NO_TOOLS]
            job["restrict_tools"] = True
        job_id = job_store.add(job)
        created = job_store.get(job_id) or {}
        sched = created.get("schedule") or {}
        result = {
            "status": "created",
            "job_id": job_id,
            "name": created.get("name") or name,
            "schedule_display": sched.get("display") or schedule,
            "next_run": created.get("next_run_at"),
            **_scheduler_runtime_state(),
        }
        if sched.get("kind") == "unparsed":
            # Saved, but the daemon skips it until the schedule is understood.
            result["will_fire"] = False
            result["schedule_error"] = (
                f"Could not interpret the schedule {schedule!r}, so this job will be skipped. "
                "Re-create it with a cron expression ('0 9 * * 1-5'), a duration ('30m'), "
                "or an ISO timestamp."
            )
        return json.dumps(result)

    elif action == "remove":
        job_id = args.get("job_id", "")
        if not job_id:
            return json.dumps({"error": "job_id required"})
        if job_store.remove(job_id):
            return json.dumps({"status": "removed", "job_id": job_id})
        return json.dumps({"error": f"Job {job_id} not found"})

    elif action == "pause":
        job_id = args.get("job_id", "")
        if job_store.update(job_id, {"status": "paused"}):
            return json.dumps({"status": "paused", "job_id": job_id})
        return json.dumps({"error": f"Job {job_id} not found"})

    elif action == "resume":
        job_id = args.get("job_id", "")
        if job_store.update(job_id, {"status": "active"}):
            return json.dumps({"status": "resumed", "job_id": job_id})
        return json.dumps({"error": f"Job {job_id} not found"})

    elif action == "update":
        job_id = args.get("job_id", "")
        updates = {}
        for key in ("prompt", "schedule", "name", "model", "script"):
            if args.get(key) is not None:
                updates[key] = args[key]
        if job_store.update(job_id, updates):
            return json.dumps({"status": "updated", "job_id": job_id})
        return json.dumps({"error": f"Job {job_id} not found"})

    elif action == "trigger":
        job_id = args.get("job_id", "")
        job = job_store.get(job_id)
        if not job:
            return json.dumps({"error": f"Job {job_id} not found"})
        # Due-job selection reads next_run_at and parses it as ISO, so a float
        # written to "next_run" was silently ignored and never triggered anything.
        from gyrfalcon.scheduler import _now_iso
        job_store.update(job_id, {"next_run_at": _now_iso()})
        return json.dumps({"status": "triggered", "job_id": job_id, **_scheduler_runtime_state()})

    return json.dumps({"error": f"Unknown action: {action}"})


# Register
registry.register(
    name="scheduler",
    toolset="scheduler",
    schema={
        "name": "scheduler",
        "description": (
            "Create and manage scheduled jobs (the Scheduler). A job runs an agent prompt "
            "or a skill on a schedule — never a raw shell command. "
            "Actions: create, list, status, update, pause, resume, remove, trigger. "
            "Use action='create' when the user asks to schedule, automate, or run something "
            "regularly; use action='status' to check whether scheduled jobs will actually fire."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "status", "update", "pause", "resume", "remove", "trigger"]},
                "job_id": {"type": "string", "description": "Job ID (for update/pause/resume/remove/trigger)"},
                "prompt": {"type": "string", "description": "Agent prompt to execute"},
                "schedule": {
                    "type": "string",
                    "description": (
                        "When to run. Accepts a cron expression ('0 9 * * 1-5'), a duration "
                        "('30m', 'every 2h'), an ISO timestamp ('2026-07-04T09:00:00'), or "
                        "natural language ('every morning at 9am', 'every weekday at 6pm')."
                    ),
                },
                "name": {"type": "string", "description": "Human-readable name"},
                "repeat": {"type": "boolean", "default": True},
                "model": {"type": "string", "description": "Model override"},
                "script": {"type": "string", "description": "Pre-run script"},
                "skills": {"type": "array", "items": {"type": "string"}},
                "no_agent": {"type": "boolean", "description": "Script-only mode", "default": False},
            },
            "required": ["action"],
        },
    },
    handler=scheduler_tool,
    emoji="⏰",
)
