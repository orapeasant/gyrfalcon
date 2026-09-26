"""Session titles must never be left empty.

Regression coverage for sessions persisting with a NULL title: the generation was
gated on conversation length (so any first turn using tools was skipped), ran in a
daemon thread, and had no fallback when the summarizing call failed.
"""

from unittest.mock import MagicMock, patch
import os

import pytest

from gyrfalcon.gyrfalcon_state import SessionDB
from gyrfalcon.run_agent import AIAgent, _clean_title, derive_fallback_title


@pytest.fixture()
def db():
    dsn = os.environ.get("GYRFALCON_TEST_PG_DSN")
    if not dsn:
        pytest.skip("Disposable PostgreSQL test DSN required")
    d = SessionDB(dsn=dsn)
    yield d
    d.close()


def _agent(db, session_id):
    a = object.__new__(AIAgent)
    a.session_db, a.session_id, a.model = db, session_id, "test-model"
    return a


def _client_returning(content):
    client = MagicMock()
    client.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content=content))
    ]
    return client


class TestDeriveFallbackTitle:
    def test_short_message_used_verbatim(self):
        assert derive_fallback_title("add a retry policy") == "add a retry policy"

    def test_blank_message_still_yields_a_title(self):
        assert derive_fallback_title("   ") == "Untitled session"
        assert derive_fallback_title("") == "Untitled session"

    def test_long_message_truncated_on_word_boundary(self):
        title = derive_fallback_title("word " * 60)
        assert len(title) <= 80
        assert title.endswith("…")
        assert "  " not in title

    def test_collapses_whitespace(self):
        assert derive_fallback_title("a\n\n  b\tc") == "a b c"


class TestCleanTitle:
    @pytest.mark.parametrize("raw,expected", [
        ('"Quoted Title."', "Quoted Title"),
        ("'Single Quoted'", "Single Quoted"),
        ("Multi\nline\ntitle", "Multi line title"),
        ("", ""),
        ("   ", ""),
    ])
    def test_normalizes(self, raw, expected):
        assert _clean_title(raw) == expected

    def test_caps_length(self):
        assert len(_clean_title("x" * 500)) == 80


class TestEnsureSessionTitle:
    def test_fallback_persists_when_llm_unavailable(self, db):
        db.create_session(session_id="s1", source="chat", model="m")
        agent = _agent(db, "s1")

        with patch.object(AIAgent, "_get_client", side_effect=RuntimeError("no creds")):
            agent._ensure_session_title("set up nightly billing export", "ok")

        assert db.get_session("s1")["title"] == "set up nightly billing export"

    def test_llm_title_replaces_fallback(self, db):
        db.create_session(session_id="s2", source="chat", model="m")
        agent = _agent(db, "s2")

        with patch.object(AIAgent, "_get_client",
                          return_value=_client_returning('"Nightly Billing Export."')):
            agent._ensure_session_title("set up nightly billing export", "ok")

        _join_title_threads()
        assert db.get_session("s2")["title"] == "Nightly Billing Export"

    def test_copilot_title_request_includes_editor_headers_when_provider_is_inferred(self, db):
        db.create_session(session_id="copilot-title", source="slack", model="gpt-4o")
        agent = _agent(db, "copilot-title")
        agent.base_url = "https://api.githubcopilot.com"
        agent.provider = None
        client = _client_returning("Slack conversation")

        with patch.object(AIAgent, "_get_client", return_value=client):
            agent._ensure_session_title("hello", "hi")

        _join_title_threads()
        headers = client.chat.completions.create.call_args.kwargs["extra_headers"]
        assert headers["Editor-Version"]
        assert headers["Editor-Plugin-Version"]

    def test_empty_llm_response_keeps_fallback(self, db):
        db.create_session(session_id="s3", source="chat", model="m")
        agent = _agent(db, "s3")

        with patch.object(AIAgent, "_get_client", return_value=_client_returning("   ")):
            agent._ensure_session_title("some request", "ok")

        _join_title_threads()
        assert db.get_session("s3")["title"] == "some request"

    def test_existing_title_is_not_overwritten(self, db):
        db.create_session(session_id="s4", source="chat", model="m")
        db.update_session_title("s4", "Human chosen title")
        agent = _agent(db, "s4")

        with patch.object(AIAgent, "_get_client",
                          return_value=_client_returning("Generated")) as spy:
            agent._ensure_session_title("anything", "ok")

        assert db.get_session("s4")["title"] == "Human chosen title"
        spy.assert_not_called()

    def test_blank_user_message_still_titled(self, db):
        db.create_session(session_id="s5", source="chat", model="m")
        agent = _agent(db, "s5")

        with patch.object(AIAgent, "_get_client", side_effect=RuntimeError("x")):
            agent._ensure_session_title("", "ok")

        assert db.get_session("s5")["title"] == "Untitled session"

    def test_title_thread_is_not_a_daemon(self, db):
        """A one-shot CLI run must not exit before the title is written."""
        db.create_session(session_id="s6", source="chat", model="m")
        agent = _agent(db, "s6")
        started = []

        real_thread = __import__("threading").Thread

        def capture(*args, **kwargs):
            started.append(kwargs.get("daemon"))
            return real_thread(*args, **kwargs)

        with patch("threading.Thread", side_effect=capture), \
             patch.object(AIAgent, "_get_client", return_value=_client_returning("T")):
            agent._ensure_session_title("hello", "ok")

        _join_title_threads()
        assert started == [False]


def _join_title_threads() -> None:
    import threading
    for t in threading.enumerate():
        if t.name == "title-gen":
            t.join(timeout=5)


class TestCapabilityPrompt:
    """The agent must not claim it lacks a capability it actually has.

    Regression: with only a generic 'execute shell commands' tool description, the
    agent replied "I don't have a tool to directly query AWS" despite aws-cli being
    installed and the terminal tool working.
    """

    def test_terminal_note_lists_detected_clis(self):
        from gyrfalcon.agent import prompt_builder as pb

        pb.detect_available_clis.cache_clear()
        with patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n if n in ("aws", "az") else None):
            note = pb._terminal_capability_note()
        pb.detect_available_clis.cache_clear()

        assert note is not None
        assert "`aws`" in note and "`az`" in note
        assert "`kubectl`" not in note   # not installed — must not be claimed

    def test_terminal_note_absent_when_no_clis(self):
        from gyrfalcon.agent import prompt_builder as pb

        pb.detect_available_clis.cache_clear()
        with patch("shutil.which", return_value=None):
            assert pb._terminal_capability_note() is None
        pb.detect_available_clis.cache_clear()

    def test_capabilities_only_cover_enabled_tools(self):
        from gyrfalcon.agent.prompt_builder import build_capabilities_prompt

        only_sched = build_capabilities_prompt({"scheduler"})
        assert "Scheduling" in only_sched
        assert "Shell and command-line tools" not in only_sched

        neither = build_capabilities_prompt({"read_file"})
        assert neither == ""

    def test_scheduler_note_present_when_enabled(self):
        from gyrfalcon.agent.prompt_builder import build_capabilities_prompt

        assert "`scheduler` tool" in build_capabilities_prompt({"scheduler"})


class TestToolFailurePrompt:
    """Tool failures must be reported as errors, not converted into refusals."""

    def test_identity_includes_failure_handling_rules(self):
        from gyrfalcon.agent.prompt_builder import DEFAULT_AGENT_IDENTITY_TEMPLATE as t

        assert "## Handling Tool Failures" in t
        assert "Show the real error text" in t
        # The manual workaround is an acceptable answer, not a fallback.
        assert "give them the exact command to run" in t

    def test_rules_reach_the_assembled_prompt(self):
        from gyrfalcon.agent.prompt_builder import build_system_prompt

        p = build_system_prompt(enabled_tools={"terminal"}, platform_name="cli",
                                skip_context_files=True, skip_memory=True)
        assert "## Handling Tool Failures" in p
        assert "not a reason to refuse" in p or "not reasons to refuse" in p
