"""Teams setup keeps the client secret out of dashboard-readable config."""

from __future__ import annotations

import io

from rich.console import Console

from gyrfalcon_cli import setup as wizard

CLIENT = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
SECRET = "new-client-secret-value"


def test_teams_setup_saves_secret_only_to_profile_env(monkeypatch):
    saved = {}
    answers = iter([CLIENT, TENANT, SECRET, "29:alice, 29:bob"])
    output = io.StringIO()
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(wizard, "save_env_value", lambda key, value: saved.__setitem__(key, value))
    config = {}

    wizard._setup_teams(Console(file=output), config)

    teams = config["gateway"]["platforms"]["teams"]
    assert saved == {"TEAMS_CLIENT_SECRET": SECRET}
    assert teams["client_id"] == CLIENT and teams["tenant_id"] == TENANT
    assert teams["toolset"] == "teams"
    assert teams["allow"] == {"users": ["29:alice", "29:bob"], "channels": []}
    assert SECRET not in repr(config) and SECRET not in output.getvalue()


def test_teams_setup_decline_changes_nothing(monkeypatch):
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: False))
    config = {"gateway": {"platforms": {}}}
    wizard._setup_teams(Console(file=io.StringIO()), config)
    assert config == {"gateway": {"platforms": {}}}


def test_teams_setup_preserves_existing_channel_and_host_settings(monkeypatch):
    answers = iter([CLIENT, TENANT, SECRET, "29:new"])
    monkeypatch.setattr(wizard.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(wizard.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(wizard, "save_env_value", lambda *a: None)
    config = {"gateway": {"platforms": {"teams": {
        "enabled": True, "host": "0.0.0.0", "port": 3999,
        "toolset": "core", "allow": {"users": ["29:old"], "channels": ["19:ops"]},
    }}}}

    wizard._setup_teams(Console(file=io.StringIO()), config)

    teams = config["gateway"]["platforms"]["teams"]
    assert teams["host"] == "0.0.0.0" and teams["port"] == 3999
    assert teams["toolset"] == "core"
    assert teams["allow"] == {"users": ["29:new"], "channels": ["19:ops"]}
