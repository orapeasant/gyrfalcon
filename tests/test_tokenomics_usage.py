"""Usage normalization — the invariant that makes cross-provider sums mean something.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.6.
"""

from types import SimpleNamespace
import os

import pytest

from gyrfalcon.pricing import calculate_cost
from gyrfalcon.tokenomics import Usage, normalize


def obj(**kwargs):
    """Build an SDK-like object; nested dicts become nested objects."""
    return SimpleNamespace(
        **{
            k: SimpleNamespace(**v) if isinstance(v, dict) else v
            for k, v in kwargs.items()
        }
    )


class TestInvariant:
    """`uncached + cache_read + cache_write` is the true prompt size."""

    def test_anthropic_sums_exclusive_fields(self):
        # Anthropic: input_tokens EXCLUDES cache, so the prompt is the sum.
        usage = normalize("anthropic", obj(
            input_tokens=100,
            cache_read_input_tokens=900,
            cache_creation_input_tokens=50,
            output_tokens=20,
        ))
        assert usage.uncached_input_tokens == 100
        assert usage.cache_read_tokens == 900
        assert usage.cache_write_tokens == 50
        assert usage.prompt_tokens == 1050

    def test_openai_subtracts_inclusive_cached(self):
        # OpenAI: prompt_tokens INCLUDES cached_tokens, so it must be split,
        # not summed — summing would report 1950 for a 1050-token prompt.
        usage = normalize("openai", obj(
            prompt_tokens=1050,
            prompt_tokens_details={"cached_tokens": 900},
            completion_tokens=20,
        ))
        assert usage.uncached_input_tokens == 150
        assert usage.cache_read_tokens == 900
        assert usage.prompt_tokens == 1050

    def test_both_providers_agree_on_the_same_prompt(self):
        """The whole point: identical prompts produce identical totals."""
        anthropic = normalize("anthropic", obj(
            input_tokens=150, cache_read_input_tokens=900,
            cache_creation_input_tokens=0, output_tokens=20,
        ))
        openai = normalize("openai", obj(
            prompt_tokens=1050,
            prompt_tokens_details={"cached_tokens": 900},
            completion_tokens=20,
        ))
        assert anthropic.prompt_tokens == openai.prompt_tokens == 1050
        assert anthropic.uncached_input_tokens == openai.uncached_input_tokens


class TestProviderShapes:
    def test_copilot_is_openai_shaped(self):
        usage = normalize("copilot", obj(
            prompt_tokens=500,
            prompt_tokens_details={"cached_tokens": 128},
            completion_tokens=40,
        ))
        assert usage.uncached_input_tokens == 372
        assert usage.cache_read_tokens == 128
        # OpenAI-shaped APIs never bill cache writes.
        assert usage.cache_write_tokens == 0

    def test_bedrock_and_vertex_use_anthropic_arithmetic(self):
        for provider in ("bedrock", "vertex", "claude"):
            usage = normalize(provider, obj(
                input_tokens=10, cache_read_input_tokens=90,
                cache_creation_input_tokens=0, output_tokens=5,
            ))
            assert usage.prompt_tokens == 100, provider

    def test_reasoning_tokens_from_openai_details(self):
        usage = normalize("openai", obj(
            prompt_tokens=100,
            completion_tokens=50,
            completion_tokens_details={"reasoning_tokens": 30},
        ))
        assert usage.reasoning_tokens == 30

    def test_no_cache_fields_is_all_uncached(self):
        """A provider that reports no caching must not lose tokens."""
        usage = normalize("openai", obj(prompt_tokens=800, completion_tokens=10))
        assert usage.uncached_input_tokens == 800
        assert usage.cache_read_tokens == 0
        assert usage.prompt_tokens == 800


class TestTolerance:
    def test_dict_payloads_work(self):
        usage = normalize("anthropic", {
            "input_tokens": 5, "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 0, "output_tokens": 2,
        })
        assert usage.prompt_tokens == 15

    def test_none_usage_is_empty_not_an_error(self):
        assert normalize("openai", None) == Usage()

    def test_none_valued_fields_count_as_zero(self):
        usage = normalize("anthropic", obj(
            input_tokens=10, cache_read_input_tokens=None,
            cache_creation_input_tokens=None, output_tokens=None,
        ))
        assert usage.prompt_tokens == 10
        assert usage.output_tokens == 0

    def test_cached_exceeding_prompt_cannot_go_negative(self):
        """A malformed payload must not corrupt every downstream sum."""
        usage = normalize("openai", obj(
            prompt_tokens=100,
            prompt_tokens_details={"cached_tokens": 500},
            completion_tokens=0,
        ))
        assert usage.uncached_input_tokens == 0
        assert usage.prompt_tokens == 100


class TestCostPricing:
    """Cost math over real catalog rates, not hardcoded multipliers."""

    MODEL = "claude-sonnet-4-5"

    def rates(self):
        from gyrfalcon.pricing import get_rates

        return get_rates(self.MODEL)

    def test_the_model_under_test_has_real_catalog_rates(self):
        """Guard: if this fell back to defaults the assertions below are vacuous."""
        r = self.rates()
        assert r.source != "default"
        assert r.cache_read is not None and r.cache_write_5m is not None

    def test_cache_read_is_much_cheaper_than_uncached_input(self):
        r = self.rates()
        uncached = calculate_cost(self.MODEL, 1000, 0)
        cached = calculate_cost(self.MODEL, 0, 0, cache_read_tokens=1000)
        assert cached == pytest.approx(r.cache_read)
        assert cached < uncached / 5

    def test_cache_write_costs_a_premium_and_1h_costs_more_than_5m(self):
        r = self.rates()
        base = calculate_cost(self.MODEL, 1000, 0)
        write_5m = calculate_cost(self.MODEL, 0, 0, cache_write_tokens=1000,
                                  cache_ttl="5m")
        write_1h = calculate_cost(self.MODEL, 0, 0, cache_write_tokens=1000,
                                  cache_ttl="1h")
        assert write_5m == pytest.approx(r.cache_write_5m)
        assert base < write_5m < write_1h

    def test_caching_beats_no_caching_by_the_second_call(self):
        """Break-even N* = (w - r)/(1 - r), which lands just above 1."""
        from gyrfalcon.pricing import break_even_calls

        tokens = 10_000
        two_uncached = 2 * calculate_cost(self.MODEL, tokens, 0)
        two_cached = (
            calculate_cost(self.MODEL, 0, 0, cache_write_tokens=tokens)
            + calculate_cost(self.MODEL, 0, 0, cache_read_tokens=tokens)
        )
        assert two_cached < two_uncached
        assert 1.0 < break_even_calls(self.rates(), "5m") < 2.0

    def test_openai_caching_can_never_lose(self):
        """No write charge means break-even is the very first reuse."""
        from gyrfalcon.pricing import break_even_calls, get_rates

        assert break_even_calls(get_rates("gpt-4o")) == 1.0

    def test_existing_callers_are_unaffected(self):
        """Cache params default off, so the old 4-arg signature is unchanged."""
        assert calculate_cost("gpt-4o", 1000, 500, 0) == calculate_cost(
            "gpt-4o", 1000, 500
        )

    def test_full_usage_prices_each_part_at_its_own_rate(self):
        r = self.rates()
        usage = normalize("anthropic", obj(
            input_tokens=1000, cache_read_input_tokens=1000,
            cache_creation_input_tokens=1000, output_tokens=0,
        ))
        cost = calculate_cost(
            self.MODEL,
            usage.uncached_input_tokens, usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        assert cost == pytest.approx(r.input + r.cache_read + r.cache_write_5m)


@pytest.mark.skipif(not os.environ.get("GYRFALCON_TEST_PG_DSN"), reason="Disposable PostgreSQL test DSN required")
class TestEndToEndCapture:
    """The wiring: a provider response must land in the DB's cache columns.

    These columns existed long before anything wrote to them, so a unit test of
    `normalize()` alone would still have passed while the bug was live. This
    drives the real response handlers against a real SessionDB.
    """

    def _agent_with_db(self, tmp_path, monkeypatch):
        from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

        monkeypatch.setenv("GYRFALCON_HOME", str(tmp_path))
        get_gyrfalcon_home.cache_clear()
        from gyrfalcon.gyrfalcon_state import SessionDB
        from gyrfalcon.run_agent import AIAgent

        dsn = os.environ["GYRFALCON_TEST_PG_DSN"]
        monkeypatch.setenv("GYRFALCON_DB_DSN", dsn)
        db = SessionDB(dsn=dsn)
        session_id = db.create_session(source="test", model="claude-sonnet-4")
        agent = AIAgent(model="claude-sonnet-4", quiet_mode=True,
                        skip_memory=True, skip_context_files=True)
        agent.session_db = db
        agent.session_id = session_id
        return agent, db, session_id

    def test_anthropic_cache_tokens_reach_the_database(self, tmp_path, monkeypatch):
        agent, db, session_id = self._agent_with_db(tmp_path, monkeypatch)

        response = SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=100,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=50,
                output_tokens=20,
            ),
            content=[SimpleNamespace(type="text", text="hello")],
        )
        agent._process_anthropic_response(response, tools=[], task_id=None,
                                          api_call_count=1)

        row = db.get_session(session_id)
        assert row["input_tokens"] == 100
        assert row["cache_read_tokens"] == 900, "cache reads were being discarded"
        assert row["cache_write_tokens"] == 50
        assert row["cost"] > 0

    def test_openai_cached_tokens_are_split_not_double_counted(self, tmp_path, monkeypatch):
        agent, db, session_id = self._agent_with_db(tmp_path, monkeypatch)
        agent.model = "gpt-4o"

        response = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=1050,
                prompt_tokens_details=SimpleNamespace(cached_tokens=900),
                completion_tokens=20,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="hi", tool_calls=None),
                finish_reason="stop",
            )],
        )
        agent._process_openai_response(response, tools=[], task_id=None,
                                       api_call_count=1)

        row = db.get_session(session_id)
        # 1050 total, of which 900 cached — the stored input must be 150, and
        # the three columns must still sum back to the real prompt size.
        assert row["input_tokens"] == 150
        assert row["cache_read_tokens"] == 900
        assert (row["input_tokens"] + row["cache_read_tokens"]
                + row["cache_write_tokens"]) == 1050
