"""Scheduler CLI command handlers."""

from __future__ import annotations

import argparse
from datetime import datetime

from rich.console import Console
from rich.table import Table

from gyrfalcon.scheduler import job_store
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("scheduler_cmd")

_USAGE = (
    "Usage: gyrfalcon scheduler <action> [options]\n\n"
    "Actions:\n"
    "  list                             List all scheduled jobs\n"
    "  add  -s <schedule> (-p <prompt> | --skill <skill>)\n"
    "                                   Add a new scheduled job\n"
    "  remove <id>                      Remove a job  (alias: delete)\n"
    "  delete <id>                      Alias for remove\n\n"
    "Schedule formats:\n"
    "  Interval : 30m  2h  1d  every 30m  every 2h\n"
    "  Cron expr: '0 * * * *'  (5-field crontab)\n"
    "  One-shot : 2026-07-01T09:00:00  (ISO datetime)\n"
)

_STATE_STYLE = {
    "scheduled": "green",
    "running":   "yellow",
    "paused":    "dim",
    "completed": "cyan",
    "failed":    "red",
}


def _fmt_dt(iso: str | None) -> str:
    """Format an ISO timestamp to a short local display string."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso


def _build_add_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gyrfalcon scheduler add",
        description="Add a new scheduled job that runs a prompt or a skill.",
        add_help=True,
    )
    parser.add_argument("--name", "-n", default="", metavar="NAME",
                        help="Human-readable job name")
    parser.add_argument("--schedule", "-s", required=True, metavar="SCHEDULE",
                        help=(
                            "Schedule — short form, cron expr, or natural language.\n"
                            "  Short  : 30m  2h  1d  every 2h\n"
                            "  Cron   : '0 9 * * 1-5'\n"
                            "  Natural: 'every weekday at 9am'  'twice a week'\n"
                            "  Once   : 2026-07-01T09:00:00"
                        ))

    exec_grp = parser.add_mutually_exclusive_group()
    exec_grp.add_argument("--prompt", "-p", default="", metavar="PROMPT",
                          help="Natural-language prompt to send to the agent on each run")
    exec_grp.add_argument("--skill", metavar="SKILL",
                          help="Skill name to invoke on each run")

    parser.add_argument("--model", "-m", default="", metavar="MODEL",
                        help="Model override (default: from config)")
    parser.add_argument("--provider", default="", metavar="PROVIDER",
                        help="Provider override")
    parser.add_argument("--repeat", type=int, default=None, metavar="N",
                        help="Run exactly N times (default: unlimited)")
    parser.add_argument("--once", action="store_true",
                        help="Run once then complete (shorthand for --repeat 1)")
    parser.add_argument("--workdir", default="", metavar="DIR",
                        help="Working directory passed to the job")
    parser.add_argument("--deliver", default="local", metavar="METHOD",
                        help="Delivery method (default: local)")
    parser.add_argument("--toolsets", default="", metavar="TOOLSETS",
                        help="Comma-separated toolsets to enable")
    parser.add_argument("--no-llm", action="store_true",
                        help="Disable LLM translation for natural-language schedules")
    return parser


def _table_for_jobs(jobs: list[dict]) -> Table:
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("ID",        style="cyan", no_wrap=True)
    table.add_column("Name")
    table.add_column("Schedule",  style="dim")
    table.add_column("Resolved",  style="dim")   # LLM-translated cron / interval
    table.add_column("Runs",      style="dim",  justify="right")
    table.add_column("State")
    table.add_column("Next Run",  style="dim",  no_wrap=True)
    table.add_column("Last Run",  style="dim",  no_wrap=True)

    for job in jobs:
        state = job.get("state", "")
        state_style = _STATE_STYLE.get(state, "")
        state_cell = f"[{state_style}]{state}[/{state_style}]" if state_style else state

        repeat = job.get("repeat", {})
        times = repeat.get("times")
        completed = repeat.get("completed", 0)
        runs_cell = f"{completed}/{times}" if times is not None else f"{completed}/∞"

        sched = job.get("schedule", {})
        raw_label = job.get("schedule_raw") or sched.get("natural", "")
        resolved_label = job.get("schedule_display") or sched.get("display", "")

        # If raw and resolved are identical (short form), only show once
        if raw_label == resolved_label:
            raw_label = ""

        table.add_row(
            job["id"],
            job.get("name", ""),
            raw_label,
            resolved_label,
            runs_cell,
            state_cell,
            _fmt_dt(job.get("next_run_at")),
            _fmt_dt(job.get("last_run_at")),
        )
    return table


def run_scheduler_cli(args: list[str] | None = None) -> None:
    """Scheduler management CLI."""
    logger.debug("Beginning of run_scheduler_cli")
    import sys

    console = Console()

    if args is None:
        args = sys.argv[2:] if len(sys.argv) > 2 else ["list"]

    action = args[0] if args else "list"

    # ── list ──────────────────────────────────────────────────────────────────
    if action == "list":
        jobs = job_store.list_all()
        if not jobs:
            console.print("[dim]No scheduled jobs configured.[/dim]")
            return
        console.print(_table_for_jobs(jobs))

    # ── add ───────────────────────────────────────────────────────────────────
    elif action == "add":
        parser = _build_add_parser()
        try:
            parsed = parser.parse_args(args[1:])
        except SystemExit:
            return

        if not parsed.prompt and not parsed.skill:
            console.print("[red]Error: supply --prompt or --skill[/red]")
            return

        use_llm = not parsed.no_llm
        repeat_times = 1 if parsed.once else parsed.repeat

        # Warn user if the schedule looks like natural language
        from gyrfalcon.scheduler import _try_regex_parse
        is_nl = _try_regex_parse(parsed.schedule) is None
        if is_nl and use_llm:
            console.print(f"[dim]⟳ Translating schedule via LLM: '{parsed.schedule}'[/dim]")

        job: dict = {
            "name":             parsed.name,
            "schedule":         parsed.schedule,
            "prompt":           parsed.prompt,
            "skill":            parsed.skill,
            "model":            parsed.model or None,
            "provider":         parsed.provider or None,
            "repeat_times":     repeat_times,
            "once":             parsed.once,
            "workdir":          parsed.workdir or None,
            "deliver":          parsed.deliver,
            "enabled_toolsets": parsed.toolsets.split(",") if parsed.toolsets else None,
        }

        from gyrfalcon.scheduler import _build_job
        built = _build_job(job, use_llm=use_llm)

        # Show translation result before persisting
        sched = built["schedule"]
        if is_nl and use_llm:
            if sched.get("kind") == "unparsed":
                console.print(
                    f"[yellow]⚠ Could not translate schedule '{parsed.schedule}' — "
                    f"job saved but will not run until schedule is fixed.[/yellow]"
                )
            else:
                resolved = sched.get("display", "")
                console.print(f"[dim]✓ Resolved to: {resolved}[/dim]")

        job_id = job_store.add(job)
        console.print(f"[green]Created scheduled job: {job_id}[/green]")

        stored = job_store.get(job_id)
        if stored:
            raw = stored.get("schedule_raw", "")
            display = stored.get("schedule_display", "")
            if raw and raw != display:
                console.print(f"  Schedule  : {raw}")
                console.print(f"  Resolved  : {display}")
            else:
                console.print(f"  Schedule  : {display}")
            if stored.get("next_run_at"):
                console.print(f"  Next run  : {_fmt_dt(stored['next_run_at'])}")
            repeat = stored.get("repeat", {})
            times = repeat.get("times")
            console.print(f"  Repeat    : {'once' if times == 1 else ('unlimited' if times is None else f'{times}x')}")
            if stored.get("skill"):
                console.print(f"  Skill     : {stored['skill']}")
            elif stored.get("prompt"):
                console.print(f"  Prompt    : {stored['prompt'][:60]}")

    # ── remove / delete ───────────────────────────────────────────────────────
    elif action in ("remove", "delete"):
        if len(args) < 2:
            console.print(f"[red]Usage: gyrfalcon scheduler {action} <job_id>[/red]")
            return
        job_id = args[1]
        if job_store.remove(job_id):
            console.print(f"[green]Removed job: {job_id}[/green]")
        else:
            console.print(f"[red]Job not found: {job_id}[/red]")

    # ── help / unknown ────────────────────────────────────────────────────────
    else:
        console.print(_USAGE)

