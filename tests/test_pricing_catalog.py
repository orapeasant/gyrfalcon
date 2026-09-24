"""Price catalog — layering, matching, and defensive parsing.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.5.2.

The catalog's refreshed layer is third-party data pulled over the network, so
most of what matters here is what happens when that data is wrong.
"""

import json

import pytest

from gyrfalcon.pricing import catalog as cat


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    """Point the profile at a temp dir and reset both caches.

    `get_gyrfalcon_home` is `lru_cache`d — correct in production, where a
    process serves one profile for its lifetime, but it means a test that only
    sets the env var silently keeps the previous test's directory.
    """
    from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

    monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
    get_gyrfalcon_home.cache_clear()
    cat.invalidate()
    yield tmp_path
    get_gyrfalcon_home.cache_clear()
    cat.invalidate()


def write_refreshed(tmp_path, models):
    path = tmp_path / "pricing" / "catalog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"models": models}), encoding="utf-8")
    cat.invalidate()


def write_overlay(tmp_path, text):
    (tmp_path / "pricing.yaml").write_text(text, encoding="utf-8")
    cat.invalidate()


class TestBundled:
    def test_the_snapshot_ships_with_real_models(self):
        loaded = cat.load()
        assert len(loaded) > 500
        assert "gpt-4o" in loaded
        assert "claude-sonnet-4-5" in loaded

    def test_anthropic_entries_carry_cache_rates(self):
        r = cat.get_rates("claude-sonnet-4-5")
        assert r.cache_read and r.cache_write_5m and r.cache_write_1h
        assert r.cache_read < r.input < r.cache_write_5m < r.cache_write_1h


class TestLayering:
    def test_refreshed_overrides_bundled(self, isolated_profile):
        before = cat.get_rates("gpt-4o").input
        write_refreshed(isolated_profile,
                        {"gpt-4o": {"provider": "openai", "input": 99.0,
                                    "output": 1.0}})
        after = cat.get_rates("gpt-4o")
        assert after.input == 99.0 != before
        assert after.source == "refreshed"

    def test_overlay_wins_over_everything(self, isolated_profile):
        write_refreshed(isolated_profile,
                        {"gpt-4o": {"provider": "openai", "input": 99.0,
                                    "output": 1.0}})
        write_overlay(isolated_profile,
                      "models:\n  gpt-4o:\n    input: 0.5\n    output: 0.9\n")
        r = cat.get_rates("gpt-4o")
        assert r.input == 0.5
        assert r.source == "overlay"

    def test_overlay_only_touches_the_models_it_names(self, isolated_profile):
        write_overlay(isolated_profile,
                      "models:\n  gpt-4o:\n    input: 0.5\n    output: 0.9\n")
        assert cat.get_rates("claude-sonnet-4-5").source == "bundled"


class TestDefensiveParsing:
    """A bad feed must degrade, never crash — and never silently zero a price."""

    def test_unreadable_refreshed_layer_falls_back_to_bundled(self, isolated_profile):
        path = isolated_profile / "pricing" / "catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not json", encoding="utf-8")
        cat.invalidate()
        assert cat.get_rates("gpt-4o").source == "bundled"

    def test_malformed_overlay_is_ignored(self, isolated_profile):
        write_overlay(isolated_profile, "models: [unclosed\n  - :::")
        assert cat.get_rates("gpt-4o").source == "bundled"

    def test_entry_without_a_price_is_dropped_not_zeroed(self, isolated_profile):
        write_refreshed(isolated_profile, {"gpt-4o": {"provider": "openai"}})
        # Falls through to the bundled entry rather than reporting free.
        assert cat.get_rates("gpt-4o").input > 0

    def test_non_numeric_rates_are_dropped(self, isolated_profile):
        write_refreshed(isolated_profile,
                        {"gpt-4o": {"input": "free", "output": "zero"}})
        assert cat.get_rates("gpt-4o").input > 0

    def test_negative_rates_are_rejected(self, isolated_profile):
        write_refreshed(isolated_profile,
                        {"gpt-4o": {"input": -5.0, "output": 1.0}})
        assert cat.get_rates("gpt-4o").input > 0

    def test_catalog_without_a_models_map_is_ignored(self, isolated_profile):
        path = isolated_profile / "pricing" / "catalog.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"nope": []}), encoding="utf-8")
        cat.invalidate()
        assert cat.get_rates("gpt-4o").source == "bundled"


class TestMatching:
    def test_exact_match(self):
        assert cat.get_rates("gpt-4o").model == "gpt-4o"

    def test_dotted_and_dashed_versions_are_equivalent(self):
        assert (cat.get_rates("claude-sonnet-4.5").input
                == cat.get_rates("claude-sonnet-4-5").input)

    def test_dated_snapshot_resolves_to_its_family(self):
        """More specific than any key -> longest matching key wins."""
        r = cat.get_rates("claude-sonnet-4-5-20250929-something-extra")
        assert r.source == "bundled"
        assert r.input == cat.get_rates("claude-sonnet-4-5").input

    def test_family_name_resolves_to_a_dated_release(self):
        """`claude-sonnet-4` exists only as `claude-sonnet-4-20250514`."""
        r = cat.get_rates("claude-sonnet-4")
        assert r.source == "bundled"
        assert r.model.startswith("claude-sonnet-4-2")

    def test_family_name_never_resolves_to_a_different_version(self):
        """The failure this guards: sonnet-4 quoted at sonnet-4-5's price."""
        assert cat.get_rates("claude-sonnet-4").model != "claude-sonnet-4-5"

    def test_unknown_model_falls_back_to_defaults(self):
        r = cat.get_rates("nonexistent-model-xyz")
        assert r.source == "default"
        assert r.input > 0

    def test_unknown_claude_keeps_anthropic_cache_semantics(self):
        """Losing the name would price cache writes as free, understating cost."""
        r = cat.get_rates("claude-future-99")
        assert r.source == "default"
        assert r.provider_is_anthropic
        assert r.effective_cache_write("5m") > 0


class TestCopilotCredits:
    def test_copilot_id_is_billed_in_credits(self):
        assert cat.get_rates("github_copilot/gpt-4o").billing == "credits"

    def test_copilot_borrows_the_underlying_vendor_rates(self):
        """The feed lists most copilot models with no rates; free would be wrong."""
        via_copilot = cat.get_rates("github_copilot/gpt-4o")
        direct = cat.get_rates("gpt-4o")
        assert via_copilot.input == direct.input > 0

    def test_a_direct_model_is_not_credit_billed(self):
        assert cat.get_rates("gpt-4o").billing == "tokens"


class TestTiering:
    def test_large_prompts_use_the_higher_tier(self):
        r = cat.get_rates("claude-sonnet-4-5")
        assert r.tier is not None, "sonnet 4.5 has >200k tiered pricing"
        assert r.at(10_000).input == r.input
        assert r.at(500_000).input > r.input

    def test_tiering_shows_up_in_the_cost(self):
        from gyrfalcon.pricing import calculate_cost

        small = calculate_cost("claude-sonnet-4-5", 100_000, 0)
        large = calculate_cost("claude-sonnet-4-5", 500_000, 0)
        assert large / 500_000 > small / 100_000


class TestRefresh:
    def test_a_network_failure_keeps_the_existing_catalog(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no network")

        monkeypatch.setattr("gyrfalcon.net.httpx_request", boom)
        result = cat.refresh()
        assert result["ok"] is False
        assert cat.get_rates("gpt-4o").input > 0

    def test_a_non_200_is_reported_not_raised(self, monkeypatch):
        class Resp:
            status_code = 503
            headers: dict = {}

        monkeypatch.setattr("gyrfalcon.net.httpx_request", lambda *a, **kw: Resp())
        assert cat.refresh()["ok"] is False

    def test_a_feed_with_no_usable_models_is_rejected(self, monkeypatch):
        class Resp:
            status_code = 200
            headers: dict = {}

            def json(self):
                return {"embed-model": {"mode": "embedding"}}

        monkeypatch.setattr("gyrfalcon.net.httpx_request", lambda *a, **kw: Resp())
        result = cat.refresh()
        assert result["ok"] is False
        assert "no usable models" in result["error"]

    def test_a_good_feed_is_converted_and_written(self, monkeypatch, isolated_profile):
        class Resp:
            status_code = 200
            headers = {"etag": "W/\"abc\""}

            def json(self):
                return {
                    "shiny-new-model": {
                        "mode": "chat",
                        "litellm_provider": "openai",
                        "input_cost_per_token": 0.000002,
                        "output_cost_per_token": 0.000008,
                        "cache_read_input_token_cost": 0.0000005,
                        "max_input_tokens": 128000,
                    },
                    "an-embedding": {"mode": "embedding"},
                }

        monkeypatch.setattr("gyrfalcon.net.httpx_request", lambda *a, **kw: Resp())
        result = cat.refresh()
        assert result["ok"] is True and result["models"] == 1

        r = cat.get_rates("shiny-new-model")
        # Feed quotes per token; the catalog stores per 1K.
        assert r.input == pytest.approx(0.002)
        assert r.output == pytest.approx(0.008)
        assert r.context_window == 128000
        assert r.source == "refreshed"

    def test_304_leaves_the_catalog_alone(self, monkeypatch):
        class Resp:
            status_code = 304
            headers: dict = {}

        monkeypatch.setattr("gyrfalcon.net.httpx_request", lambda *a, **kw: Resp())
        result = cat.refresh()
        assert result["ok"] is True and result["unchanged"] is True
