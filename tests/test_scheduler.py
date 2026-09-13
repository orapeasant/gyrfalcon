"""Tests for the scheduler job store and CLI commands (new schema)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── fixtures / helpers ────────────────────────────────────────────────────────

@pytest.fixture()
def isolated_store(tmp_path):
    """Provide (store, patcher) backed by tmp_path for full test isolation."""
    (tmp_path / "scheduler").mkdir(exist_ok=True)

    from gyrfalcon.scheduler import JobStore

    with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
        store = JobStore()

    @contextmanager
    def _patcher():
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            yield

    return store, _patcher


def _run_scheduler_cli(args: list[str], tmp_path: Path) -> str:
    """Run run_scheduler_cli with isolated store + mocked console; return captured text."""
    from gyrfalcon.scheduler import JobStore
    from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli

    (tmp_path / "scheduler").mkdir(exist_ok=True)
    captured: list[str] = []
    mock_console = MagicMock()
    mock_console.print.side_effect = lambda *a, **kw: captured.append(str(a[0]) if a else "")

    with (
        patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path),
        patch("gyrfalcon_cli.scheduler_cmd.Console", return_value=mock_console),
    ):
        store = JobStore()
        with patch("gyrfalcon_cli.scheduler_cmd.job_store", store):
            run_scheduler_cli(args)

    return "\n".join(captured)


# ── parse_schedule ────────────────────────────────────────────────────────────

class TestParseSchedule:

    def test_duration_minutes(self):
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("30m")
        assert s["kind"] == "interval"
        assert s["minutes"] == 30
        assert s["display"] == "every 30m"

    def test_duration_hours(self):
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("2h")
        assert s["kind"] == "interval"
        assert s["minutes"] == 120
        assert s["display"] == "every 2h"

    def test_duration_days(self):
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("1d")
        assert s["kind"] == "interval"
        assert s["minutes"] == 1440

    def test_every_format(self):
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("every 30m")
        assert s["kind"] == "interval"
        assert s["minutes"] == 30

    def test_cron_expression(self):
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("0 * * * *")
        assert s["kind"] == "cron"
        assert s["expression"] == "0 * * * *"

    def test_natural_language_no_llm_gives_unparsed(self):
        """Unrecognized NL without LLM → kind=unparsed, no crash."""
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("every weekday at 9am", use_llm=False)
        assert s["kind"] == "unparsed"
        assert s["natural"] == "every weekday at 9am"

    def test_natural_language_with_llm_mock(self):
        """LLM returns '0 9 * * 1-5' → parsed as cron, natural preserved."""
        from gyrfalcon.scheduler import parse_schedule
        with patch("gyrfalcon.scheduler._llm_translate_schedule",
                   return_value={"kind": "cron", "expression": "0 9 * * 1-5",
                                 "display": "0 9 * * 1-5", "natural": "every weekday at 9am"}):
            s = parse_schedule("every weekday at 9am", use_llm=True)
        assert s["kind"] == "cron"
        assert s["expression"] == "0 9 * * 1-5"
        assert s["natural"] == "every weekday at 9am"

    def test_natural_language_interval_llm_mock(self):
        """LLM returns 'every 2h' → parsed as interval."""
        from gyrfalcon.scheduler import parse_schedule
        with patch("gyrfalcon.scheduler._llm_translate_schedule",
                   return_value={"kind": "interval", "minutes": 120,
                                 "display": "every 2h", "natural": "twice a day roughly"}):
            s = parse_schedule("twice a day roughly", use_llm=True)
        assert s["kind"] == "interval"
        assert s["minutes"] == 120

    def test_natural_preserves_original_in_natural_field(self):
        """natural field always stores the original raw input."""
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("30m")
        assert s["natural"] == "30m"

    def test_every_word_variants(self):
        """'hourly', 'daily', 'weekly' are recognized without LLM."""
        from gyrfalcon.scheduler import parse_schedule
        assert parse_schedule("hourly")["kind"] == "interval"
        assert parse_schedule("daily")["kind"] == "interval"

    def test_parse_with_full_unit_name(self):
        """'every 2 hours', '3 days' — unit word form recognized."""
        from gyrfalcon.scheduler import parse_schedule
        s = parse_schedule("every 2 hours")
        assert s["kind"] == "interval"
        assert s["minutes"] == 120

        s2 = parse_schedule("3 days")
        assert s2["kind"] == "interval"
        assert s2["minutes"] == 4320


# ── next_run_iso ──────────────────────────────────────────────────────────────

class TestNextRunIso:

    def test_interval_schedule(self):
        from gyrfalcon.scheduler import next_run_iso
        now = datetime.now(timezone.utc).astimezone()
        result = next_run_iso({"kind": "interval", "minutes": 60})
        nxt = datetime.fromisoformat(result)
        delta = (nxt - now).total_seconds()
        assert 3590 < delta < 3610

    def test_once_schedule(self):
        from gyrfalcon.scheduler import next_run_iso
        future = "2099-01-01T00:00:00"
        result = next_run_iso({"kind": "once", "at": future})
        assert result is not None
        nxt = datetime.fromisoformat(result)
        assert nxt > datetime.now(timezone.utc).astimezone()

    def test_cron_schedule(self):
        pytest.importorskip("croniter")
        from gyrfalcon.scheduler import next_run_iso
        result = next_run_iso({"kind": "cron", "expression": "0 * * * *"})
        nxt = datetime.fromisoformat(result)
        assert nxt > datetime.now(timezone.utc).astimezone()


# ── _build_runtime_prompt ─────────────────────────────────────────────────────

class TestBuildRuntimePrompt:
    """Verify the scheduler enriches natural-language prompts with context."""

    def _make_scheduler(self):
        from gyrfalcon.scheduler import Scheduler
        s = object.__new__(Scheduler)
        return s

    def test_prompt_contains_job_name(self):
        sched = self._make_scheduler()
        job = {"id": "abc123", "name": "Daily check", "schedule": {"kind": "interval", "minutes": 1440},
               "schedule_display": "every 1d", "schedule_raw": "daily",
               "prompt": "Summarise today's logs", "repeat": {"times": None, "completed": 0}}
        result = sched._build_runtime_prompt(job)
        assert "Daily check" in result

    def test_prompt_contains_schedule_display(self):
        sched = self._make_scheduler()
        job = {"id": "abc123", "name": "x", "schedule": {"kind": "interval", "minutes": 60},
               "schedule_display": "every 1h", "schedule_raw": "every hour",
               "prompt": "Run diagnostics", "repeat": {"times": None, "completed": 2}}
        result = sched._build_runtime_prompt(job)
        assert "every 1h" in result

    def test_prompt_contains_run_number(self):
        sched = self._make_scheduler()
        job = {"id": "abc123", "name": "x", "schedule": {"kind": "interval", "minutes": 60},
               "schedule_display": "every 1h", "schedule_raw": "",
               "prompt": "Ping", "repeat": {"times": 5, "completed": 2}}
        result = sched._build_runtime_prompt(job)
        assert "Run #3/5" in result

    def test_original_prompt_preserved(self):
        sched = self._make_scheduler()
        job = {"id": "abc123", "name": "x", "schedule": {"kind": "interval", "minutes": 60},
               "schedule_display": "every 1h", "schedule_raw": "",
               "prompt": "Check disk space on /data", "repeat": {"times": None, "completed": 0}}
        result = sched._build_runtime_prompt(job)
        assert "Check disk space on /data" in result

    def test_prompt_contains_timestamp(self):
        sched = self._make_scheduler()
        job = {"id": "abc123", "name": "x", "schedule": {"kind": "interval", "minutes": 60},
               "schedule_display": "every 1h", "schedule_raw": "",
               "prompt": "Do something", "repeat": {"times": None, "completed": 0}}
        result = sched._build_runtime_prompt(job)
        # Should contain a date in YYYY-MM-DD format
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2}", result)


# ── JobStore unit tests ───────────────────────────────────────────────────────

class TestJobStore:

    def test_add_returns_12char_id(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "hello"})
        assert len(job_id) == 12
        assert job_id.isalnum()

    def test_add_persists_job(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            store.add({"name": "myjob", "schedule": "30m", "prompt": "ping"})
            assert len(store.list_all()) == 1
            assert store.list_all()[0]["name"] == "myjob"

    def test_add_builds_canonical_schema(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"name": "x", "schedule": "1h", "prompt": "y"})
            job = store.get(job_id)

        assert job["state"] == "scheduled"
        assert job["enabled"] is True
        assert job["last_run_at"] is None
        assert job["next_run_at"] is not None
        assert job["created_at"] is not None
        assert isinstance(job["schedule"], dict)
        assert job["schedule"]["kind"] == "interval"
        assert job["schedule"]["minutes"] == 60
        assert job["schedule_display"] == "every 1h"
        assert job["schedule_raw"] == "1h"          # natural field preserved
        assert job["repeat"] == {"times": None, "completed": 0}
        assert job["deliver"] == "local"
        assert job["skill"] is None
        assert job["prompt"] == "y"

    def test_add_structured_schedule_interval(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "every 30m", "prompt": "p"})
            job = store.get(job_id)
        assert job["schedule"]["kind"] == "interval"
        assert job["schedule"]["minutes"] == 30
        assert job["schedule_display"] == "every 30m"

    def test_add_next_run_at_is_iso_future(self, isolated_store):
        store, patcher = isolated_store
        before = datetime.now(timezone.utc).astimezone()
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "y"})
            job = store.get(job_id)
        nxt = datetime.fromisoformat(job["next_run_at"])
        assert nxt > before

    def test_add_with_skill(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "skill": "log-checker"})
            job = store.get(job_id)
        assert job["skill"] == "log-checker"
        assert job["prompt"] == ""

    def test_add_once_flag(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "run once", "once": True})
            job = store.get(job_id)
        assert job["repeat"]["times"] == 1

    def test_add_repeat_times(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "p", "repeat_times": 5})
            job = store.get(job_id)
        assert job["repeat"]["times"] == 5
        assert job["repeat"]["completed"] == 0

    def test_file_envelope_format(self, tmp_path):
        """jobs.json must have {"jobs": [...], "updated_at": "..."}"""
        import json
        (tmp_path / "scheduler").mkdir(exist_ok=True)
        from gyrfalcon.scheduler import JobStore
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            store.add({"schedule": "1h", "prompt": "p"})
            raw = json.loads((tmp_path / "scheduler" / "jobs.json").read_text())
        assert "jobs" in raw
        assert "updated_at" in raw
        assert isinstance(raw["jobs"], list)
        assert len(raw["jobs"]) == 1

    def test_list_empty(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            assert store.list_all() == []

    def test_list_multiple(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            store.add({"name": "a", "schedule": "1h", "prompt": "a"})
            store.add({"name": "b", "schedule": "2h", "prompt": "b"})
            assert len(store.list_all()) == 2

    def test_remove_existing(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "y"})
            assert store.remove(job_id) is True
            assert store.list_all() == []

    def test_remove_missing(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            assert store.remove("nonexistent") is False

    def test_get_existing(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"name": "z", "schedule": "1d", "prompt": "q"})
            job = store.get(job_id)
        assert job is not None
        assert job["name"] == "z"

    def test_get_missing(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            assert store.get("no-such-id") is None

    def test_update(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1h", "prompt": "p"})
            assert store.update(job_id, {"state": "paused"}) is True
            assert store.get(job_id)["state"] == "paused"

    def test_update_missing(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            assert store.update("none", {"state": "x"}) is False

    def test_get_due_jobs(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1m", "prompt": "p"})
            # Force next_run_at into the past
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            store.update(job_id, {"next_run_at": past})
            due = store.get_due_jobs()
        assert any(j["id"] == job_id for j in due)

    def test_get_due_jobs_skips_disabled(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1m", "prompt": "p"})
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            store.update(job_id, {"next_run_at": past, "enabled": False})
            due = store.get_due_jobs()
        assert not any(j["id"] == job_id for j in due)

    def test_get_due_jobs_skips_non_scheduled_state(self, isolated_store):
        store, patcher = isolated_store
        with patcher():
            job_id = store.add({"schedule": "1m", "prompt": "p"})
            past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            store.update(job_id, {"next_run_at": past, "state": "paused"})
            due = store.get_due_jobs()
        assert not any(j["id"] == job_id for j in due)


# ── CLI command tests ─────────────────────────────────────────────────────────

class TestSchedulerCLI:

    def test_list_empty(self, tmp_path):
        output = _run_scheduler_cli(["list"], tmp_path)
        assert "No scheduled jobs" in output

    def test_add_with_prompt(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--name", "Test", "--schedule", "1h", "--prompt", "ping"],
            tmp_path,
        )
        assert "Created scheduled job" in output

    def test_add_with_skill(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--schedule", "every 30m", "--skill", "log-checker"],
            tmp_path,
        )
        assert "Created scheduled job" in output

    def test_add_shows_next_run(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--schedule", "1h", "--prompt", "ping"],
            tmp_path,
        )
        assert "Next run" in output

    def test_add_shows_schedule_display(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--schedule", "2h", "--prompt", "x"],
            tmp_path,
        )
        assert "every 2h" in output

    def test_add_once_flag(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--schedule", "1h", "--prompt", "once", "--once"],
            tmp_path,
        )
        assert "once" in output

    def test_add_repeat_n(self, tmp_path):
        output = _run_scheduler_cli(
            ["add", "--schedule", "1h", "--prompt", "p", "--repeat", "3"],
            tmp_path,
        )
        assert "Created scheduled job" in output

    def test_add_requires_prompt_or_skill(self, tmp_path):
        output = _run_scheduler_cli(["add", "--schedule", "1h"], tmp_path)
        assert "Created scheduled job" not in output

    def test_add_missing_schedule_returns_gracefully(self, tmp_path):
        output = _run_scheduler_cli(["add", "--prompt", "hello"], tmp_path)
        assert "Created scheduled job" not in output

    def test_remove_existing(self, tmp_path):
        from gyrfalcon.scheduler import JobStore
        (tmp_path / "scheduler").mkdir(exist_ok=True)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            job_id = store.add({"schedule": "1h", "prompt": "p"})

        captured: list[str] = []
        mock_console = MagicMock()
        mock_console.print.side_effect = lambda *a, **kw: captured.append(str(a[0]) if a else "")
        from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli
        with (
            patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path),
            patch("gyrfalcon_cli.scheduler_cmd.Console", return_value=mock_console),
            patch("gyrfalcon_cli.scheduler_cmd.job_store", store),
        ):
            run_scheduler_cli(["remove", job_id])
        assert "Removed job" in "\n".join(captured)

    def test_delete_alias(self, tmp_path):
        from gyrfalcon.scheduler import JobStore
        (tmp_path / "scheduler").mkdir(exist_ok=True)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            job_id = store.add({"schedule": "1h", "prompt": "p"})

        captured: list[str] = []
        mock_console = MagicMock()
        mock_console.print.side_effect = lambda *a, **kw: captured.append(str(a[0]) if a else "")
        from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli
        with (
            patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path),
            patch("gyrfalcon_cli.scheduler_cmd.Console", return_value=mock_console),
            patch("gyrfalcon_cli.scheduler_cmd.job_store", store),
        ):
            run_scheduler_cli(["delete", job_id])
        assert "Removed job" in "\n".join(captured)

    def test_remove_missing_id_shows_usage(self, tmp_path):
        output = _run_scheduler_cli(["remove"], tmp_path)
        assert "Usage:" in output

    def test_unknown_action_shows_usage(self, tmp_path):
        output = _run_scheduler_cli(["bogus"], tmp_path)
        assert "Usage:" in output


# ── slash command tests ───────────────────────────────────────────────────────

def _make_cli_with_store(tmp_path: Path):
    from gyrfalcon.scheduler import JobStore
    from gyrfalcon_cli.cli import GyrfalconCLI

    (tmp_path / "scheduler").mkdir(exist_ok=True)
    captured: list[str] = []
    mock_console = MagicMock()
    mock_console.print.side_effect = lambda *a, **kw: captured.append(str(a[0]) if a else "")

    cli = object.__new__(GyrfalconCLI)
    cli.console = mock_console
    cli.model = "test-model"
    cli.provider = "test"

    with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
        store = JobStore()

    return cli, store, captured


class TestSchedulerSlashCommand:

    def test_list_empty(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("")
        assert "No scheduled jobs" in "\n".join(captured)

    def test_add_prompt_job(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("add --schedule 1h --prompt 'ping'")
        assert "Created scheduled job" in "\n".join(captured)

    def test_add_skill_job(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("add --schedule 30m --skill log-checker")
        assert "Created scheduled job" in "\n".join(captured)

    def test_add_shows_schedule_display(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("add --schedule 2h --prompt check")
        assert "every 2h" in "\n".join(captured)

    def test_add_requires_prompt_or_skill(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("add --schedule 1h")
        assert "Created scheduled job" not in "\n".join(captured)

    def test_remove(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            job_id = store.add({"schedule": "1h", "prompt": "p"})
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler(f"remove {job_id}")
        assert "Removed job" in "\n".join(captured)

    def test_delete_alias(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            job_id = store.add({"schedule": "1h", "prompt": "p"})
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler(f"delete {job_id}")
        assert "Removed job" in "\n".join(captured)

    def test_remove_no_id_shows_usage(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("remove")
        assert "Usage:" in "\n".join(captured)

    def test_unknown_shows_help(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            cli._cmd_scheduler("bogus")
        assert "Scheduler commands" in "\n".join(captured)

    def test_process_command_routes_scheduler(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)
        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            result = cli._process_command("/cron list")
        assert result is True
        assert "No scheduled jobs" in "\n".join(captured)


# ── End-to-end ────────────────────────────────────────────────────────────────

class TestSchedulerEndToEnd:

    def test_full_lifecycle_store(self, tmp_path):
        from gyrfalcon.scheduler import JobStore
        (tmp_path / "scheduler").mkdir(exist_ok=True)

        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()

            job_id = store.add({
                "name":     "E2E Job",
                "schedule": "every 2h",
                "prompt":   "Summarise today's logs",
            })
            assert len(job_id) == 12

            jobs = store.list_all()
            assert len(jobs) == 1
            job = jobs[0]
            assert job["name"] == "E2E Job"
            assert job["state"] == "scheduled"
            assert job["enabled"] is True
            assert job["schedule"]["kind"] == "interval"
            assert job["schedule"]["minutes"] == 120
            assert job["schedule_display"] == "every 2h"
            assert job["repeat"] == {"times": None, "completed": 0}
            assert job["next_run_at"] is not None
            nxt = datetime.fromisoformat(job["next_run_at"])
            assert nxt > datetime.now(timezone.utc).astimezone()

            assert store.remove(job_id) is True
            assert store.list_all() == []

    def test_cli_add_then_store_verify(self, tmp_path):
        from gyrfalcon.scheduler import JobStore
        from gyrfalcon_cli.scheduler_cmd import run_scheduler_cli
        (tmp_path / "scheduler").mkdir(exist_ok=True)
        mock_console = MagicMock()

        with (
            patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path),
            patch("gyrfalcon_cli.scheduler_cmd.Console", return_value=mock_console),
        ):
            run_scheduler_cli([
                "add",
                "--name",     "Server check",
                "--schedule", "every 120m",
                "--prompt",   "Check server status",
                "--deliver",  "local",
            ])
            store = JobStore()
            jobs = store.list_all()

        assert len(jobs) == 1
        job = jobs[0]
        assert job["name"] == "Server check"
        assert job["prompt"] == "Check server status"
        assert job["schedule"]["kind"] == "interval"
        assert job["schedule"]["minutes"] == 120
        assert job["schedule_display"] == "every 120m"
        assert job["deliver"] == "local"
        assert job["state"] == "scheduled"
        assert job["enabled"] is True
        assert job["repeat"] == {"times": None, "completed": 0}
        assert job["next_run_at"] is not None

    def test_slash_e2e_add_list_delete(self, tmp_path):
        cli, store, captured = _make_cli_with_store(tmp_path)

        with patch("gyrfalcon.scheduler.job_store", store), \
             patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):

            cli._cmd_scheduler("add --name 'Slash E2E' --schedule 2h --prompt 'hello'")
            assert "Created scheduled job" in "\n".join(captured)

            jobs = store.list_all()
            assert len(jobs) == 1
            assert jobs[0]["schedule"]["kind"] == "interval"
            job_id = jobs[0]["id"]

            captured.clear()
            cli._cmd_scheduler(f"delete {job_id}")
            assert "Removed job" in "\n".join(captured)
            assert store.list_all() == []



class TestSchedulerDirMigration:
    """Legacy ~/.gyrfalcon/cron state is adopted, not orphaned."""

    def test_adopts_legacy_cron_dir(self, tmp_path):
        from gyrfalcon.scheduler import _get_scheduler_dir

        legacy = tmp_path / "cron"
        legacy.mkdir()
        (legacy / "jobs.json").write_text('{"jobs": []}')

        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            d = _get_scheduler_dir()

        assert d == tmp_path / "scheduler"
        assert (d / "jobs.json").read_text() == '{"jobs": []}'
        assert not legacy.exists()

    def test_prefers_existing_scheduler_dir(self, tmp_path):
        """A stale cron/ dir must not clobber real scheduler/ state."""
        from gyrfalcon.scheduler import _get_scheduler_dir

        (tmp_path / "cron").mkdir()
        (tmp_path / "cron" / "jobs.json").write_text("STALE")
        (tmp_path / "scheduler").mkdir()
        (tmp_path / "scheduler" / "jobs.json").write_text("CURRENT")

        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            d = _get_scheduler_dir()

        assert (d / "jobs.json").read_text() == "CURRENT"


class TestSchedulerToolReporting:
    """create/status tell the truth about whether a job will actually fire."""

    def _tool(self, tmp_path, args):
        import json as _json

        from gyrfalcon.scheduler import JobStore
        from gyrfalcon.tools import scheduler_tool as st

        (tmp_path / "scheduler").mkdir(exist_ok=True)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            with patch.object(st, "job_store", store), \
                 patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
                return _json.loads(st.scheduler_tool(args))

    def test_create_reports_it_will_not_fire_when_stopped(self, tmp_path):
        r = self._tool(tmp_path, {"action": "create", "name": "n",
                                  "prompt": "p", "schedule": "1h"})
        assert r["status"] == "created"
        assert r["will_fire"] is False
        assert "gyrfalcon gateway" in r["note"]
        assert r["next_run"]

    def test_create_flags_unparseable_schedule(self, tmp_path):
        with patch("gyrfalcon.scheduler._llm_translate_schedule", return_value=None):
            r = self._tool(tmp_path, {"action": "create", "name": "n",
                                      "prompt": "p", "schedule": "sometime nextish"})
        assert r["will_fire"] is False
        assert "schedule_error" in r

    def test_status_action(self, tmp_path):
        r = self._tool(tmp_path, {"action": "status"})
        assert r["job_count"] == 0
        assert r["scheduler_enabled"] is True

    def test_trigger_sets_due_marker_the_scheduler_reads(self, tmp_path):
        """Regression: trigger wrote a float to 'next_run', which get_due_jobs ignores."""
        import json as _json
        from datetime import datetime

        from gyrfalcon.scheduler import JobStore
        from gyrfalcon.tools import scheduler_tool as st

        (tmp_path / "scheduler").mkdir(exist_ok=True)
        with patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
            store = JobStore()
            with patch.object(st, "job_store", store), \
                 patch("gyrfalcon.scheduler.get_gyrfalcon_home", return_value=tmp_path):
                created = _json.loads(st.scheduler_tool(
                    {"action": "create", "name": "n", "prompt": "p", "schedule": "1h"}))
                job_id = created["job_id"]
                _json.loads(st.scheduler_tool({"action": "trigger", "job_id": job_id}))

                job = store.get(job_id)
                # Parses as ISO and is now due, so the scheduler will pick it up.
                assert datetime.fromisoformat(job["next_run_at"])
                assert job_id in [j["id"] for j in store.get_due_jobs()]


class TestProfileFlagScoping:
    """`-p` after a subcommand is that subcommand's flag, not --profile.

    Regression: `scheduler add -p "<prompt>"` was read as `--profile <prompt>`,
    silently redirecting GYRFALCON_HOME and filing jobs under a bogus profile.
    """

    def _home_after(self, argv):
        import sys as _sys

        from gyrfalcon_cli.main import _apply_profile_override

        with patch.object(_sys, "argv", argv), \
             patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GYRFALCON_HOME", None)
            _apply_profile_override()
            return env.get("GYRFALCON_HOME")

    def test_prompt_flag_is_not_a_profile(self):
        # Not a profile switch, but GYRFALCON_HOME is still defaulted to
        # ~/.gyrfalcon (not left unset) — the point being tested is that it
        # is NOT redirected to a bogus "-p ping"-named profile.
        home = self._home_after(
            ["gyrfalcon", "scheduler", "add", "-s", "1h", "-p", "ping"]
        )
        assert home is not None and home.endswith("/.gyrfalcon") and "ping" not in home

    def test_global_profile_flag_still_applies(self):
        home = self._home_after(["gyrfalcon", "--profile", "work", "scheduler", "list"])
        assert home is not None and home.endswith("/.gyrfalcon-profiles/work")

    def test_short_global_profile_flag_still_applies(self):
        home = self._home_after(["gyrfalcon", "-p", "work", "chat"])
        assert home is not None and home.endswith("/.gyrfalcon-profiles/work")

    def test_equals_form_still_applies(self):
        home = self._home_after(["gyrfalcon", "--profile=work", "scheduler", "list"])
        assert home is not None and home.endswith("/.gyrfalcon-profiles/work")
