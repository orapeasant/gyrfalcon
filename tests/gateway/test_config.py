"""Gateway config: one typed view of `gateway:`, and an allowlist that fails closed."""

from __future__ import annotations

import yaml

from gyrfalcon.gateway.config import AllowList, GatewayConfig, PlatformConfig
from gyrfalcon.gateway.platforms.base import SessionSource


class TestAllowList:
    """With identity off this is the only access control in the system."""

    def test_empty_admits_nobody(self):
        allow = AllowList.from_config({})
        assert not allow.permits("U1", "D1", "dm")
        assert not allow.permits("U1", "C1", "channel")

    def test_missing_block_admits_nobody(self):
        assert not AllowList.from_config(None).permits("U1", "D1", "dm")

    def test_a_listed_user_may_dm(self):
        assert AllowList.from_config({"users": ["U1"]}).permits("U1", "D1", "dm")

    def test_an_unlisted_user_may_not_dm(self):
        assert not AllowList.from_config({"users": ["U1"]}).permits("U2", "D1", "dm")

    def test_a_channel_needs_both_the_user_and_the_channel(self):
        allow = AllowList.from_config({"users": ["U1"], "channels": ["C1"]})
        assert allow.permits("U1", "C1", "channel")
        assert not allow.permits("U1", "C2", "channel"), "allowed user, unlisted channel"
        assert not allow.permits("U9", "C1", "channel"), "listed channel, unlisted user"

    def test_listing_only_channels_does_not_open_them_to_everyone(self):
        # Adding the bot to a channel must not admit whoever is in it.
        allow = AllowList.from_config({"channels": ["C1"]})
        assert not allow.permits("U1", "C1", "channel")

    def test_no_channel_list_means_dms_only(self):
        allow = AllowList.from_config({"users": ["U1"]})
        assert allow.permits("U1", "D1", "dm")
        assert not allow.permits("U1", "C1", "channel")

    def test_an_unknown_chat_type_is_treated_as_shared(self):
        assert not AllowList.from_config({"users": ["U1"]}).permits("U1", "X1", "")

    def test_a_blank_user_id_is_never_admitted(self):
        assert not AllowList.from_config({"users": ["U1"]}).permits("", "D1", "dm")

    def test_yaml_ints_and_scalars_are_normalised(self):
        raw = yaml.safe_load("users: 12345\nchannels: [67890, C1]")
        allow = AllowList.from_config(raw)
        assert allow.permits("12345", "67890", "channel")

    def test_a_string_is_a_single_id_not_a_list_of_characters(self):
        allow = AllowList.from_config({"users": "U01ABC"})
        assert allow.users == frozenset({"U01ABC"})

    def test_blank_entries_do_not_become_a_wildcard(self):
        assert AllowList.from_config({"users": ["", "  ", None]}).users == frozenset()


class TestPlatformConfig:
    def test_adapter_specific_keys_pass_through_untyped(self):
        p = PlatformConfig.from_config("slack", {"mode": "socket", "stream": True, "allow": {"users": ["U1"]}})
        assert p.get("mode") == "socket"
        assert p.get("stream") is True
        assert p.get("missing", 7) == 7

    def test_shared_keys_are_typed_and_not_left_in_extra(self):
        p = PlatformConfig.from_config("slack", {"enabled": False, "toolset": "core", "max_iterations": "9"})
        assert p.enabled is False and p.toolset == "core" and p.max_iterations == 9
        assert "toolset" not in p.extra and "enabled" not in p.extra

    def test_garbage_numbers_fall_back_instead_of_crashing(self):
        p = PlatformConfig.from_config("x", {"max_iterations": "lots", "rate_limit_rpm": -3})
        assert p.max_iterations is None
        assert p.rate_limit_rpm == 30

    def test_replying_to_strangers_is_off_by_default(self):
        assert PlatformConfig.from_config("x", {}).reply_on_deny is False

    def test_a_non_mapping_block_is_tolerated(self):
        assert PlatformConfig.from_config("x", None).enabled is True


class TestGatewayConfig:
    def test_empty_and_missing_sections_load(self):
        assert GatewayConfig.from_dict(None).platforms == {}
        assert GatewayConfig.from_dict({}).agent_cache_size == 128

    def test_platforms_are_parsed(self):
        cfg = GatewayConfig.from_dict({"platforms": {"slack": {"allow": {"users": ["U1"]}}}})
        assert cfg.platforms["slack"].allow.users == frozenset({"U1"})

    def test_loads_from_the_real_config_path(self, tmp_path, monkeypatch):
        # D1: config.yaml is the home; gateway.yaml is not read at all.
        from gyrfalcon import config as config_mod
        from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

        monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
        get_gyrfalcon_home.cache_clear()
        monkeypatch.setattr(config_mod, "_config_cache", None)
        monkeypatch.setattr(config_mod, "_config_mtime", None)
        try:
            (tmp_path / "config.yaml").write_text(
                yaml.dump({"gateway": {"platforms": {"slack": {"allow": {"users": ["U9"]}}}}})
            )
            (tmp_path / "gateway.yaml").write_text(yaml.dump({"platforms": {"decoy": {}}}))
            cfg = GatewayConfig.load()
            assert list(cfg.platforms) == ["slack"]
            assert cfg.platforms["slack"].allow.users == frozenset({"U9"})
        finally:
            get_gyrfalcon_home.cache_clear()
            config_mod._config_cache = None
            config_mod._config_mtime = None


class TestRoutingRules:
    def _cfg(self, rules):
        return GatewayConfig.from_dict({"routing_rules": rules})

    def _src(self, **kw):
        return SessionSource(platform="slack", chat_id=kw.pop("chat_id", "C0123"), **kw)

    def test_a_channel_name_pattern_matches_with_or_without_the_hash(self):
        cfg = self._cfg([{"pattern": "^#ops", "toolset": "readonly"}])
        assert cfg.route(self._src(chat_name="ops-alerts").route_candidates()).toolset == "readonly"
        cfg = self._cfg([{"pattern": "^ops", "toolset": "readonly"}])
        assert cfg.route(self._src(chat_name="ops-alerts").route_candidates()) is not None

    def test_a_channel_id_pattern_matches(self):
        cfg = self._cfg([{"pattern": "^C0123$", "model": "m"}])
        assert cfg.route(self._src().route_candidates()).model == "m"

    def test_the_first_matching_rule_wins(self):
        cfg = self._cfg([{"pattern": "ops", "toolset": "a"}, {"pattern": "ops", "toolset": "b"}])
        assert cfg.route(self._src(chat_name="ops").route_candidates()).toolset == "a"

    def test_no_match_is_none(self):
        cfg = self._cfg([{"pattern": "^#ops"}])
        assert cfg.route(self._src(chat_name="random").route_candidates()) is None

    def test_an_invalid_regex_is_dropped_not_fatal(self):
        cfg = self._cfg([{"pattern": "([unclosed", "toolset": "x"}, {"pattern": "ok", "toolset": "y"}])
        assert [r.pattern for r in cfg.routing_rules] == ["ok"]

    def test_a_rule_without_a_pattern_is_dropped(self):
        assert self._cfg([{"toolset": "x"}, "nonsense"]).routing_rules == []


class TestSessionKey:
    def test_a_thread_is_the_session_and_the_user_is_not_part_of_it(self):
        a = SessionSource(platform="slack", chat_id="C1", thread_id="111.1", user_id="U1")
        b = SessionSource(platform="slack", chat_id="C1", thread_id="111.1", user_id="U2")
        c = SessionSource(platform="slack", chat_id="C1", thread_id="222.2", user_id="U1")
        assert a.session_key == b.session_key == "slack:C1:111.1"
        assert a.session_key != c.session_key
