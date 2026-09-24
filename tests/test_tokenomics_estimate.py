"""Estimator and the /api/tokenomics/* routes.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.5.4-5.
"""

import pytest

from gyrfalcon.pricing import ModelRates
from gyrfalcon.tokenomics.estimate import (
    DEFAULT_OUTPUT_TOKENS,
    _reuse_table,
    estimate,
    estimate_model,
)


def has_tiktoken() -> bool:
    try:
        import tiktoken  # noqa: F401

        return True
    except ImportError:
        return False


needs_tiktoken = pytest.mark.skipif(not has_tiktoken(),
                                    reason="needs the `tokenizers` extra")

ANTHROPIC = ModelRates(model="claude-x", provider="anthropic", input=0.003,
                       output=0.015, cache_read=0.0003, cache_write_5m=0.00375,
                       cache_write_1h=0.006)
OPENAI = ModelRates(model="gpt-x", provider="openai", input=0.0025,
                    output=0.01, cache_read=0.000625)


class TestReuseTable:
    """Caching is a curve. The first call is the part that is easy to get wrong."""

    def test_anthropic_first_call_costs_the_write_premium(self):
        points = _reuse_table(ANTHROPIC, 1000, "5m", [1])
        assert points[0].cached_cost == pytest.approx(0.00375)
        assert points[0].uncached_cost == pytest.approx(0.003)
        # Caching the first call is a *loss* — that is the whole point of
        # showing a break-even rather than a single "cache cost".
        assert points[0].saving < 0

    def test_openai_first_call_is_ordinary_input_not_free(self):
        """No write *charge* is not the same as a free first call."""
        points = _reuse_table(OPENAI, 1000, "5m", [1])
        assert points[0].cached_cost == pytest.approx(0.0025)
        assert points[0].cached_cost == pytest.approx(points[0].uncached_cost)

    def test_savings_grow_with_reuse(self):
        points = _reuse_table(ANTHROPIC, 10_000, "5m", [1, 2, 5, 10, 50])
        savings = [p.saving for p in points]
        assert savings == sorted(savings)
        assert savings[0] < 0 < savings[-1]

    def test_caching_wins_by_the_second_call_on_anthropic(self):
        two = _reuse_table(ANTHROPIC, 10_000, "5m", [2])[0]
        assert two.cached_cost < two.uncached_cost

    def test_one_hour_ttl_costs_more_up_front(self):
        five_min = _reuse_table(ANTHROPIC, 10_000, "5m", [1])[0]
        one_hour = _reuse_table(ANTHROPIC, 10_000, "1h", [1])[0]
        assert one_hour.cached_cost > five_min.cached_cost

    def test_zero_and_negative_reuse_counts_are_dropped(self):
        assert _reuse_table(ANTHROPIC, 1000, "5m", [0, -3, 2]) != []
        assert [p.calls for p in _reuse_table(ANTHROPIC, 1000, "5m", [0, -3, 2])] == [2]


@needs_tiktoken
class TestEstimateModel:
    def test_costs_scale_with_content(self):
        small = estimate_model("gpt-4o", "hello")
        large = estimate_model("gpt-4o", "hello " * 5000)
        assert large.input_tokens > small.input_tokens
        assert large.input_cost > small.input_cost

    def test_output_tokens_are_priced(self):
        none = estimate_model("gpt-4o", "hi", output_tokens=0)
        some = estimate_model("gpt-4o", "hi", output_tokens=1000)
        assert none.output_cost == 0
        assert some.output_cost > 0
        assert some.total_cost > none.total_cost

    def test_context_usage_is_reported(self):
        row = estimate_model("gpt-4o", "word " * 1000)
        assert row.context_window == 128000
        assert row.fits_context is True
        assert 0 < row.context_used_pct < 100

    def test_oversized_input_is_flagged_as_not_fitting(self):
        row = estimate_model("gpt-4o", "word " * 200_000)
        assert row.fits_context is False

    def test_an_uncountable_model_reports_no_cost(self):
        row = estimate_model("wat-9000", "hello")
        assert row.available is False
        assert row.input_tokens == 0
        assert row.total_cost == 0
        assert row.detail

    def test_method_and_tokenizer_are_carried_through(self):
        row = estimate_model("gpt-4o", "hello")
        assert row.method == "exact"
        assert row.tokenizer


@needs_tiktoken
class TestEstimate:
    MODELS = ["gpt-4o", "wat-9000"]

    def test_returns_one_row_per_model(self):
        result = estimate("hello", self.MODELS, include_agent_prompt=False)
        assert [m.model for m in result.models] == self.MODELS

    def test_uncountable_models_raise_a_warning(self):
        result = estimate("hello", self.MODELS, include_agent_prompt=False)
        assert any("no available tokenizer" in w for w in result.warnings)

    def test_agent_prompt_dominates_a_short_input(self):
        """Why the toggle defaults on (§19.10 Q5)."""
        bare = estimate("hi", ["gpt-4o"], include_agent_prompt=False)
        full = estimate("hi", ["gpt-4o"], include_agent_prompt=True)
        assert bare.models[0].input_tokens < 5
        assert full.models[0].input_tokens > 500
        assert full.overhead_tokens > 500

    def test_overhead_is_zero_when_excluded(self):
        assert estimate("hi", ["gpt-4o"], include_agent_prompt=False).overhead_tokens == 0

    def test_defaults_are_recorded_in_the_result(self):
        result = estimate("hi", ["gpt-4o"], include_agent_prompt=False)
        assert result.output_tokens == DEFAULT_OUTPUT_TOKENS
        assert result.cache_ttl == "5m"
        assert result.input_chars == 2

    def test_serializes_to_json_safe_dicts(self):
        import json

        payload = estimate("hi", ["gpt-4o"], include_agent_prompt=False).as_dict()
        json.dumps(payload)                      # must not raise
        assert "saving" in payload["models"][0]["reuses"][0]


class TestRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        import gyrfalcon_cli.web_server as web_server

        return TestClient(web_server.app), {
            "X-Gyrfalcon-Session-Token": web_server._session_token
        }

    def test_models_endpoint_lists_priced_models(self, client):
        c, h = client
        r = c.get("/api/tokenomics/models?q=gpt-4o&limit=5", headers=h)
        assert r.status_code == 200
        assert r.json()["total"] > 0

    def test_models_endpoint_requires_a_token(self, client):
        c, _ = client
        assert c.get("/api/tokenomics/models").status_code == 401

    @needs_tiktoken
    def test_estimate_endpoint(self, client):
        c, h = client
        r = c.post("/api/tokenomics/estimate", headers=h, json={
            "text": "hello world", "models": ["gpt-4o"],
            "include_agent_prompt": False,
        })
        assert r.status_code == 200
        assert r.json()["models"][0]["input_tokens"] > 0

    def test_estimate_requires_models(self, client):
        c, h = client
        r = c.post("/api/tokenomics/estimate", headers=h, json={"text": "hi"})
        assert r.status_code == 400

    def test_estimate_requires_content(self, client):
        c, h = client
        r = c.post("/api/tokenomics/estimate", headers=h, json={"models": ["gpt-4o"]})
        assert r.status_code == 400

    def test_estimate_caps_the_model_count(self, client):
        """A picker with 400 entries must not become a 400-model request."""
        c, h = client
        r = c.post("/api/tokenomics/estimate", headers=h,
                   json={"text": "hi", "models": [f"m{i}" for i in range(20)]})
        assert r.status_code == 400

    def test_unknown_upload_is_404(self, client):
        c, h = client
        r = c.post("/api/tokenomics/estimate", headers=h,
                   json={"upload_id": "nope", "models": ["gpt-4o"]})
        assert r.status_code == 404

    def test_upload_then_estimate(self, client):
        c, h = client
        r = c.post("/api/tokenomics/upload", headers=h,
                   files={"file": ("a.md", b"# Title\n\nSome content here.",
                                   "text/markdown")})
        assert r.status_code == 200
        body = r.json()
        assert body["chars"] > 0 and body["empty"] is False

        r2 = c.post("/api/tokenomics/estimate", headers=h, json={
            "upload_id": body["id"], "models": ["gpt-4o"],
            "include_agent_prompt": False,
        })
        assert r2.status_code == 200
        assert r2.json()["input_chars"] == body["chars"]

    def test_oversize_upload_is_rejected(self, client):
        from gyrfalcon.documents import MAX_BYTES

        c, h = client
        r = c.post("/api/tokenomics/upload", headers=h,
                   files={"file": ("big.txt", b"x" * (MAX_BYTES + 1), "text/plain")})
        assert r.status_code == 413
