"""Scheduler system — job store and scheduler."""

from __future__ import annotations

import re
import uuid
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from filelock import FileLock

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.gyrfalcon_logging import get_logger
from gyrfalcon.utils import atomic_json_write, safe_json_loads

logger = get_logger("scheduler")


# ── helpers ───────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    """Current local time as an ISO-8601 string with UTC-offset."""
    return datetime.now(timezone.utc).astimezone().isoformat()


def _short_id() -> str:
    """12-char hex job ID (e.g. '67ff34d269b8')."""
    return uuid.uuid4().hex[:12]


def _get_scheduler_dir() -> Path:
    """Scheduler state dir. Adopts a legacy ~/.gyrfalcon/cron directory if present."""
    home = get_gyrfalcon_home()
    d = home / "scheduler"
    if not d.exists():
        legacy = home / "cron"
        if legacy.is_dir():
            legacy.rename(d)
            logger.info("Migrated scheduler state: cron/ -> scheduler/")
    d.mkdir(exist_ok=True)
    return d


def _get_jobs_file() -> Path:
    return _get_scheduler_dir() / "jobs.json"


def _get_heartbeat_file() -> Path:
    return _get_scheduler_dir() / "heartbeat"


# A tick runs each minute; allow two missed beats before calling it stopped.
_HEARTBEAT_STALE_SECONDS = 150


def _write_heartbeat() -> None:
    """Record that the scheduler loop is alive, so other processes can see it."""
    try:
        _get_heartbeat_file().write_text(str(time.time()))
    except OSError as e:
        logger.warning(f"Could not write scheduler heartbeat: {e}")


def is_scheduler_running() -> bool:
    """Whether a scheduler loop is ticking — in this process or any other.

    The loop lives in the gateway process, so an in-memory flag is not enough:
    the dashboard and CLI need the on-disk heartbeat to see it.
    """
    if scheduler._running:
        return True
    try:
        age = time.time() - float(_get_heartbeat_file().read_text().strip())
    except (OSError, ValueError):
        return False
    return age < _HEARTBEAT_STALE_SECONDS


def _get_output_dir() -> Path:
    d = _get_scheduler_dir() / "output"
    d.mkdir(exist_ok=True)
    return d


# ── schedule parsing ──────────────────────────────────────────────────────────

_INTERVAL_RE = re.compile(
    r"^(?:every\s+)?(\d+)\s*(second|minute|hour|day|week|s|m|h|d|w)s?$",
    re.IGNORECASE,
)
_BARE_UNIT_RE = re.compile(
    r"^(?:every\s+)?(second|minute|hour(?:ly)?|dai(?:ly)?|day|week(?:ly)?)s?$",
    re.IGNORECASE,
)

_BARE_UNIT_NORM: dict[str, str] = {
    "second": "second", "minute": "minute",
    "hour": "hour", "hourly": "hour",
    "day": "day", "dai": "day", "daily": "day",
    "week": "week", "weekly": "week",
}

_UNIT_MINUTES: dict[str, int] = {
    "s": 0,      # seconds — stored as sub-minute interval, treated as 1m min
    "second": 0,
    "m": 1, "minute": 1,
    "h": 60, "hour": 60,
    "d": 1440, "day": 1440,
    "w": 10080, "week": 10080,
}

_NL_SCHEDULE_SYSTEM = """\
You are a schedule parser. Convert the user's natural-language schedule description
into exactly one of these three formats — nothing else:

1. INTERVAL   → output the string:  every <N><unit>
   where unit is one of: m (minutes), h (hours), d (days)
   Examples: "every 30m", "every 2h", "every 1d"

2. CRON       → output a standard 5-field cron expression (min hour dom month dow)
   Examples: "0 9 * * 1-5", "30 8 * * 1", "0 */4 * * *"

3. ONCE       → output an ISO-8601 datetime string
   Example: "2026-07-04T09:00:00"

Rules:
- Output ONLY the schedule string, no explanation, no quotes.
- Use 24-hour time.
- Assume the user's local timezone.
- If unsure, prefer a cron expression.
"""


def _try_regex_parse(s: str) -> Optional[dict]:
    """Fast-path: parse without LLM. Returns None if not recognized."""
    # Bare unit: "hourly", "daily", "weekly", "every minute", "every hour"
    m = _BARE_UNIT_RE.match(s)
    if m:
        raw_unit = m.group(1).lower()
        unit = _BARE_UNIT_NORM.get(raw_unit, raw_unit)
        minutes = max(_UNIT_MINUTES.get(unit, 1), 1)
        _DISPLAY = {"second": "every second", "minute": "every minute",
                    "hour": "hourly", "day": "daily", "week": "weekly"}
        return {"kind": "interval", "minutes": minutes,
                "display": _DISPLAY.get(unit, f"every {unit}")}

    # "30m", "2h", "every 30m", "every 2 hours"
    m = _INTERVAL_RE.match(s)
    if m:
        value, unit = int(m.group(1)), m.group(2).lower()
        minutes = max(_UNIT_MINUTES.get(unit, 1), 1) * value
        short_unit = {"second": "s", "minute": "m", "hour": "h",
                      "day": "d", "week": "w"}.get(unit, unit)
        return {"kind": "interval", "minutes": minutes,
                "display": f"every {value}{short_unit}"}

    # 5-field cron expression
    parts = s.split()
    if len(parts) == 5 and all(
        re.match(r"^[\d\*\/\-\,]+$", p) for p in parts
    ):
        return {"kind": "cron", "expression": s, "display": s}

    # ISO datetime one-shot
    try:
        datetime.fromisoformat(s)
        return {"kind": "once", "at": s, "display": s}
    except (ValueError, TypeError):
        pass

    return None  # needs LLM


def _llm_translate_schedule(natural: str) -> Optional[dict]:
    """Ask the configured LLM to convert natural language into a schedule string."""
    try:
        from gyrfalcon.config import cfg_get
        from gyrfalcon.providers.copilot import get_copilot_credentials, is_authenticated
        import httpx, json as _json

        model = cfg_get("model.name", "gpt-4o-mini")
        api_key: Optional[str] = None
        base_url: Optional[str] = None

        # Try Copilot first, then env OPENAI_API_KEY
        if is_authenticated():
            base_url, api_key = get_copilot_credentials()

        if not api_key:
            import os
            api_key = os.environ.get("OPENAI_API_KEY", "")
            base_url = "https://api.openai.com/v1"

        if not api_key:
            return None

        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _NL_SCHEDULE_SYSTEM},
                    {"role": "user",   "content": natural},
                ],
                "temperature": 0,
                "max_tokens": 40,
            },
            timeout=15,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        logger.debug(f"LLM schedule translation: '{natural}' -> '{raw}'")

        parsed = _try_regex_parse(raw)
        if parsed:
            parsed["natural"] = natural
            return parsed

        # LLM returned something we can't parse — log and return None
        logger.warning(f"LLM schedule result '{raw}' not parseable, using fallback")
        return None

    except Exception as e:
        logger.warning(f"LLM schedule translation failed: {e}")
        return None


def parse_schedule(raw: str, use_llm: bool = False) -> dict:
    """Convert a schedule string (or natural language) into a structured object.

    The returned dict always contains:
        kind          : "interval" | "cron" | "once"
        display       : human-readable label
        natural       : original raw input (preserved)

    Plus kind-specific fields:
        interval  → minutes (int)
        cron      → expression (str, 5-field)
        once      → at (ISO datetime str)

    Parameters
    ----------
    raw:
        Any of: "30m", "every 2h", "0 9 * * 1-5", "2026-07-01T09:00",
        "every weekday at 9am", "twice a week on Monday and Thursday", …
    use_llm:
        When True (default during job creation), unrecognized phrases are
        sent to the LLM for translation.  Set False in tests / offline use.
    """
    s = raw.strip()
    result = _try_regex_parse(s)

    if result is None and use_llm:
        result = _llm_translate_schedule(s)

    if result is None:
        # Hard fallback: store as unknown cron — daemon will skip until fixed
        logger.warning(f"Could not parse schedule '{s}', stored as unparsed")
        result = {"kind": "unparsed", "raw": s, "display": s}

    result.setdefault("natural", s)
    return result


def next_run_iso(schedule: dict, after: datetime | None = None) -> Optional[str]:
    """Calculate next run ISO timestamp from a structured schedule object."""
    kind = schedule.get("kind")
    base = after or datetime.now(timezone.utc).astimezone()

    if kind == "interval":
        minutes = schedule.get("minutes", 0)
        if not minutes:
            return None
        return (base + timedelta(minutes=minutes)).isoformat()

    if kind == "cron":
        try:
            from croniter import croniter
            it = croniter(schedule["expression"], base)
            nxt = it.get_next(datetime)
            if nxt.tzinfo is None:
                nxt = nxt.replace(tzinfo=timezone.utc).astimezone()
            return nxt.isoformat()
        except Exception:
            return None

    if kind == "once":
        try:
            at = datetime.fromisoformat(schedule["at"])
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc).astimezone()
            return at.isoformat()
        except (ValueError, TypeError):
            return None

    return None  # "unparsed" or unknown kind


# ── JobStore ──────────────────────────────────────────────────────────────────

class JobStore:
    """Persistent scheduled job store.

    File format::

        {
          "jobs": [ <job>, ... ],
          "updated_at": "<ISO timestamp>"
        }

    Each job matches the canonical schema (see ``_build_job``).
    """

    def __init__(self):
        self._lock = FileLock(str(_get_scheduler_dir() / "jobs.lock"), timeout=10)

    # ── persistence ───────────────────────────────────────────────────────────

    def _load(self) -> dict:
        jobs_file = _get_jobs_file()
        if not jobs_file.exists():
            return {"jobs": [], "updated_at": _now_iso()}
        data = safe_json_loads(jobs_file.read_text(), {})
        if isinstance(data, list):
            return {"jobs": data, "updated_at": _now_iso()}
        if not isinstance(data, dict) or "jobs" not in data:
            return {"jobs": [], "updated_at": _now_iso()}
        return data

    def _save(self, store: dict) -> None:
        store["updated_at"] = _now_iso()
        atomic_json_write(_get_jobs_file(), store)

    # ── public API ────────────────────────────────────────────────────────────

    def add(self, job: dict) -> str:
        """Persist a new job and return its ID."""
        with self._lock:
            store = self._load()
            new_job = _build_job(job)
            store["jobs"].append(new_job)
            self._save(store)
            return new_job["id"]

    def remove(self, job_id: str) -> bool:
        with self._lock:
            store = self._load()
            before = len(store["jobs"])
            store["jobs"] = [j for j in store["jobs"] if j["id"] != job_id]
            if len(store["jobs"]) < before:
                self._save(store)
                return True
            return False

    def update(self, job_id: str, updates: dict) -> bool:
        with self._lock:
            store = self._load()
            for job in store["jobs"]:
                if job["id"] == job_id:
                    job.update(updates)
                    self._save(store)
                    return True
            return False

    def get(self, job_id: str) -> Optional[dict]:
        for job in self._load()["jobs"]:
            if job["id"] == job_id:
                return job
        return None

    def list_all(self) -> list[dict]:
        return self._load()["jobs"]

    def get_due_jobs(self, now: datetime | None = None) -> list[dict]:
        """Return enabled+scheduled jobs whose next_run_at has passed."""
        now = now or datetime.now(timezone.utc).astimezone()
        due = []
        for job in self._load()["jobs"]:
            if not job.get("enabled", True):
                continue
            if job.get("state") not in ("scheduled", None):
                continue
            next_run = job.get("next_run_at")
            if not next_run:
                continue
            try:
                nxt = datetime.fromisoformat(next_run)
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
                if nxt <= now:
                    due.append(job)
            except (ValueError, TypeError):
                pass
        return due


def _build_job(src: dict, use_llm: bool = True) -> dict:
    """Construct a canonical job dict from a partial input dict.

    The ``schedule`` field in *src* may be:
    - A raw string (short form, cron expr, ISO datetime, or natural language).
    - An already-parsed dict (pass-through).

    When *use_llm* is True (default), natural-language strings that fail
    regex parsing are sent to the LLM for translation before storage.
    """
    schedule_raw = src.get("schedule", "")
    if isinstance(schedule_raw, str):
        schedule_obj = parse_schedule(schedule_raw, use_llm=use_llm)
    else:
        schedule_obj = schedule_raw  # already a dict

    repeat_times = src.get("repeat_times")
    if src.get("once"):
        repeat_times = 1

    return {
        "id":                  src.get("id") or _short_id(),
        "name":                src.get("name", ""),
        "prompt":              src.get("prompt", ""),
        "skills":              src.get("skills", []),
        "skill":               src.get("skill"),
        "model":               src.get("model") or None,
        "provider":            src.get("provider") or None,
        "base_url":            src.get("base_url") or None,
        "script":              src.get("script") or None,
        "no_agent":            bool(src.get("no_agent", False)),
        "context_from":        src.get("context_from"),
        "schedule":            schedule_obj,
        "schedule_display":    schedule_obj.get("display", ""),
        "schedule_raw":        schedule_obj.get("natural", ""),
        "repeat": {
            "times":     repeat_times,
            "completed": 0,
        },
        "enabled":             bool(src.get("enabled", True)),
        "state":               "scheduled",
        "paused_at":           None,
        "paused_reason":       None,
        "created_at":          _now_iso(),
        "next_run_at":         next_run_iso(schedule_obj),
        "last_run_at":         None,
        "last_status":         None,
        "last_error":          None,
        "last_delivery_error": None,
        "deliver":             src.get("deliver", "local"),
        "origin":              src.get("origin"),
        "enabled_toolsets":    src.get("enabled_toolsets") or None,
        # Set for jobs created by a restricted agent (a chat platform): the run
        # then enforces its toolset at dispatch instead of only filtering the
        # schema, and a restricted agent may only edit jobs carrying this flag.
        "restrict_tools":      bool(src.get("restrict_tools", False)),
        "workdir":             src.get("workdir") or None,
    }


# ── Scheduler ─────────────────────────────────────────────────────────────────

class Scheduler:
    """Tick-based scheduler. Executes due jobs by running a prompt or a skill."""

    def __init__(self):
        self._store = JobStore()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tick_lock = FileLock(str(_get_scheduler_dir() / ".tick.lock"), timeout=5)

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()
        logger.info("Scheduler started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Scheduler stopped")

    def tick(self) -> None:
        try:
            with self._tick_lock:
                for job in self._store.get_due_jobs():
                    self._execute_job(job)
        except Exception as e:
            logger.error(f"Scheduler tick failed: {e}")

    def _loop(self) -> None:
        while self._running:
            _write_heartbeat()
            self.tick()
            for _ in range(60):
                if not self._running:
                    break
                time.sleep(1)

    def _execute_job(self, job: dict) -> None:
        """Run a single due job: either a prompt or a skill."""
        job_id = job["id"]
        logger.info(f"Executing scheduled job {job_id} ({job.get('name', '')})")

        now_iso = _now_iso()
        self._store.update(job_id, {"state": "running", "last_run_at": now_iso})

        repeat = job.get("repeat", {})
        repeat_times = repeat.get("times")
        completed = repeat.get("completed", 0) + 1

        try:
            if job.get("skill"):
                output = self._run_skill(job)
            elif job.get("prompt"):
                output = self._run_prompt(job)
            else:
                output = "(no prompt or skill configured)"

            updates: dict = {
                "last_run_at":  now_iso,
                "last_status":  "success",
                "last_error":   None,
                "repeat":       {"times": repeat_times, "completed": completed},
            }
            if repeat_times is not None and completed >= repeat_times:
                updates["state"] = "completed"
                updates["next_run_at"] = None
            else:
                updates["state"] = "scheduled"
                updates["next_run_at"] = next_run_iso(job["schedule"])

            self._store.update(job_id, updates)
            self._save_output(job_id, output)
            self._deliver_output(job, output)
            logger.info(f"Scheduled job {job_id} completed ({completed} run(s))")

        except Exception as e:
            logger.error(f"Scheduled job {job_id} failed: {e}")
            self._store.update(job_id, {
                "state":       "failed",
                "last_error":  str(e),
                "last_status": "error",
            })

    def _build_runtime_prompt(self, job: dict) -> str:
        """Enrich the stored natural-language prompt with runtime context.

        Prepends a brief system block so the agent knows *when* and *why*
        it is being invoked without changing the user's original intent.
        """
        now = datetime.now(timezone.utc).astimezone()
        ts = now.strftime("%Y-%m-%d %H:%M %Z")
        job_name = job.get("name") or job["id"]
        schedule_display = job.get("schedule_display") or job.get("schedule_raw", "")
        repeat = job.get("repeat", {})
        run_n = repeat.get("completed", 0) + 1
        run_of = f"/{repeat['times']}" if repeat.get("times") else ""

        context_header = (
            f"[Scheduled job: {job_name}]\n"
            f"Run #{run_n}{run_of} | Triggered at: {ts} | Schedule: {schedule_display}\n"
            "---\n"
        )
        return context_header + job["prompt"]

    def _run_prompt(self, job: dict) -> str:
        """Invoke the agent with the enriched natural-language prompt."""
        from gyrfalcon.run_agent import AIAgent
        from gyrfalcon.config import cfg_get

        agent = AIAgent(
            model=job.get("model") or cfg_get("model.name", ""),
            provider=job.get("provider") or cfg_get("provider.active", "copilot"),
            base_url=job.get("base_url") or None,
            max_iterations=30,
            quiet_mode=True,
            skip_memory=True,
            platform="scheduler",
            enabled_toolsets=job.get("enabled_toolsets"),
            restrict_tools=bool(job.get("restrict_tools")),
        )
        enriched = self._build_runtime_prompt(job)
        return agent.chat(enriched)

    def _run_skill(self, job: dict) -> str:
        """Invoke a named skill."""
        from gyrfalcon.tools.skills_tool import run_skill
        return run_skill(job["skill"], job.get("prompt", ""))

    def _deliver_output(self, job: dict, output: str) -> None:
        """Send a job's result to its `deliver` target, if it has one.

        Delivery failing must not fail the job: the work is done and its output
        is already on disk. The reason is recorded on the job instead, in the
        `last_delivery_error` field that has existed — and stayed None — since
        before anything could deliver anything.
        """
        target = (job.get("deliver") or "").strip()
        if not target or target.lower() == "local":
            return

        from gyrfalcon.gateway.delivery import deliver_from_anywhere

        name = job.get("name") or job["id"]
        result = deliver_from_anywhere(target, f"*{name}*\n\n{output}")
        if result.ok:
            logger.info(f"Delivered job {job['id']} output to {target}")
        else:
            logger.warning(f"Job {job['id']} ran, but delivery to {target} failed: {result.detail}")
        self._store.update(job["id"], {"last_delivery_error": None if result.ok else result.detail})

    def _save_output(self, job_id: str, output: str) -> None:
        out_dir = _get_output_dir() / job_id
        out_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        (out_dir / f"{ts}.md").write_text(output)


# ── global singletons ─────────────────────────────────────────────────────────

job_store = JobStore()
scheduler = Scheduler()
