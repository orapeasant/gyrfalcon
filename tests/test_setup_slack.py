"""`gyrfalcon setup` step 4: connect Slack. Tokens go to .env, never config.yaml."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from gyrfalcon_cli import setup as wizard

BOT = "xoxb-fixture-only"
APP = "xapp-1-A0123-3333333333-0123456789abcdef"


@pytest.fixture()
def rig(monkeypatch):
    """Scripted prompts + captured .env writes. Returns (run, saved_env, output)."""
    saved: dict[str, str] = {}
    out = io.StringIO()
    monkeypatch.setattr(wizard, "save_env_value", lambda k, v: saved.__setitem__(k, v))

    def run(config, confirm=True, answers=()):
        replies = iter(answers)
        monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: confirm))
        monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: next(replies)))
        wizard._setup_slack(Console(file=out, width=200, force_terminal=False), config)
        return config

    return run, saved, out


def test_declining_changes_nothing(rig):
    run, saved, out = rig
    config = {"gateway": {"platforms": {}}}
    run(config, confirm=False)
    assert config == {"gateway": {"platforms": {}}} and saved == {}


def test_a_full_run_writes_tokens_to_env_and_the_rest_to_config(rig):
    run, saved, out = rig
    config = run({}, answers=[BOT, APP, "U01ABC, U02DEF"])
    assert saved == {"SLACK_BOT_TOKEN": BOT, "SLACK_APP_TOKEN": APP}
    slack = config["gateway"]["platforms"]["slack"]
    assert slack == {"enabled": True, "mode": "socket", "toolset": "slack",
                     "allow": {"users": ["U01ABC", "U02DEF"], "channels": []}}


def test_no_token_ever_lands_in_the_config_file(rig):
    # The dashboard's config API returns config.yaml, so a token there is served over HTTP.
    run, saved, out = rig
    config = run({}, answers=[BOT, APP, "U01ABC"])
    assert BOT not in repr(config) and APP not in repr(config)
    assert BOT not in out.getvalue() and APP not in out.getvalue()


def test_swapped_tokens_are_rejected_and_nothing_is_saved(rig):
    run, saved, out = rig
    config = run({}, answers=[APP, BOT])
    assert saved == {} and "gateway" not in config
    assert "Nothing was saved" in out.getvalue()


@pytest.mark.parametrize("bot,app", [("", APP), (BOT, ""), ("not-a-token", "also-not")])
def test_missing_or_malformed_tokens_are_rejected(rig, bot, app):
    run, saved, out = rig
    run({}, answers=[bot, app])
    assert saved == {}


def test_an_empty_user_list_is_saved_but_warned_about(rig):
    run, saved, out = rig
    config = run({}, answers=[BOT, APP, "  ,  "])
    assert config["gateway"]["platforms"]["slack"]["allow"]["users"] == []
    assert "refuse every message" in out.getvalue()


def test_id_parsing_tolerates_spaces_semicolons_and_blanks():
    assert wizard._parse_ids("U1, U2;U3 ,, ") == ["U1", "U2", "U3"]
    assert wizard._parse_ids("") == []


def test_reconfiguring_keeps_the_settings_the_wizard_does_not_ask_about(rig):
    run, saved, out = rig
    existing = {"gateway": {"platforms": {"slack": {
        "enabled": True, "toolset": "core", "respond_to": {"threads": False},
        "allow": {"users": ["UOLD"], "channels": ["C1"]}, "rate_limit_rpm": 5}}}}
    run(existing, answers=[BOT, APP, "UNEW"])
    slack = existing["gateway"]["platforms"]["slack"]
    assert slack["allow"] == {"users": ["UNEW"], "channels": ["C1"]}
    assert slack["respond_to"] == {"threads": False} and slack["rate_limit_rpm"] == 5
    assert slack["toolset"] == "core", "an operator's deliberate toolset choice is not overwritten"


def test_reconfiguring_asks_a_different_question(rig, monkeypatch):
    run, saved, out = rig
    asked = []
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda q, **k: asked.append(q) or False))
    wizard._setup_slack(Console(file=out), {"gateway": {"platforms": {"slack": {"enabled": True}}}})
    wizard._setup_slack(Console(file=out), {})
    assert "already configured" in asked[0] and "Connect Gyrfalcon to Slack" in asked[1]


def test_existing_users_are_offered_as_the_default(rig, monkeypatch):
    seen = {}
    replies = iter([BOT, APP])

    def ask(prompt, **kw):
        if "member ID" in prompt:
            seen["default"] = kw.get("default")
            return kw.get("default", "")
        return next(replies)
    rig  # fixture side effects (save_env_value patch)
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(ask))
    config = {"gateway": {"platforms": {"slack": {"enabled": True, "allow": {"users": ["UA", "UB"]}}}}}
    wizard._setup_slack(Console(file=io.StringIO()), config)
    assert seen["default"] == "UA, UB"
    assert config["gateway"]["platforms"]["slack"]["allow"]["users"] == ["UA", "UB"]


def test_the_wizard_calls_the_slack_step(monkeypatch):
    calls = []
    for name in ("_setup_provider", "_setup_agent", "_setup_security"):
        monkeypatch.setattr(wizard, name, lambda *a, **k: None)
    monkeypatch.setattr(wizard, "_setup_slack", lambda console, config: calls.append("slack"))
    monkeypatch.setattr(wizard, "_setup_teams", lambda console, config: None)
    monkeypatch.setattr(wizard, "save_config", lambda c: None)
    monkeypatch.setattr(wizard, "load_config", lambda: {})
    wizard.run_setup()
    assert calls == ["slack"]


def test_a_missing_sdk_is_pointed_out_but_does_not_block_setup(rig, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "slack_sdk":
            raise ImportError
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake)
    run, saved, out = rig
    config = run({}, answers=[BOT, APP, "U1"])
    assert "uv sync --extra slack" in out.getvalue()
    assert config["gateway"]["platforms"]["slack"]["enabled"] is True
