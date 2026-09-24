"""Tokenizer registry — routing, exact counts, and honest degradation.

Spec: `docs/spec/gyrfalcon/19-tokenomics.md` §19.5.1.

Tests needing the optional `tokenizers` extra skip when it is absent, the way
the PostgreSQL tests skip without a DSN — `uv run pytest` must stay green on a
bare install. Run them with:

    uv run --extra tokenizers pytest tests/test_tokenizers.py
"""

import json

import pytest

from gyrfalcon.tokenizers import (
    API,
    EXACT,
    UNAVAILABLE,
    TokenCount,
    count_request,
    count_text,
    describe,
)
from gyrfalcon.tokenizers import registry as reg


def has_tiktoken() -> bool:
    try:
        import tiktoken  # noqa: F401

        return True
    except ImportError:
        return False


def has_hf() -> bool:
    try:
        import tokenizers  # noqa: F401

        return True
    except ImportError:
        return False


needs_tiktoken = pytest.mark.skipif(not has_tiktoken(),
                                    reason="needs the `tokenizers` extra (tiktoken)")
needs_hf = pytest.mark.skipif(not has_hf(),
                              reason="needs the `tokenizers` extra (huggingface)")

SAMPLE = "The quick brown fox jumps over the lazy dog. " * 10


class TestRouting:
    """Which family a model name lands in — no dependencies needed."""

    @pytest.mark.parametrize("model,family", [
        ("gpt-4o", "openai"),
        ("gpt-3.5-turbo", "openai"),
        ("o3-mini", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
        ("claude-opus-4-5-20250101", "anthropic"),
        ("gemini-2.5-pro", "google"),
        ("llama-3-70b", "open-weight"),
        ("qwen-2.5-72b", "open-weight"),
        ("deepseek-v3", "open-weight"),
    ])
    def test_model_routes_to_expected_family(self, model, family):
        assert describe(model)["family"] == family

    def test_provider_prefix_is_stripped(self):
        """`github_copilot/gpt-4o` is still a GPT model."""
        assert describe("github_copilot/gpt-4o")["family"] == "openai"

    def test_unknown_model_has_no_family(self):
        assert describe("wat-9000")["family"] == ""


class TestHonestDegradation:
    """The core promise: never guess, always explain."""

    def test_unknown_model_is_unavailable_not_guessed(self):
        result = count_text("wat-9000", SAMPLE)
        assert result.method == UNAVAILABLE
        assert result.tokens == 0
        assert not result.is_known
        assert "wat-9000" in result.detail

    def test_empty_text_is_zero_and_exact(self):
        assert count_text("gpt-4o", "") == TokenCount(0, EXACT, "empty")

    def test_missing_dependency_explains_how_to_fix_it(self, monkeypatch):
        """A missing extra must name the install command, not just fail."""
        reg._tiktoken_encoding.cache_clear()
        import builtins

        real_import = builtins.__import__

        def no_tiktoken(name, *args, **kwargs):
            if name == "tiktoken":
                raise ImportError("no tiktoken")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_tiktoken)
        result = count_text("gpt-4o", SAMPLE)
        reg._tiktoken_encoding.cache_clear()

        assert result.method == UNAVAILABLE
        assert "--extra tokenizers" in result.detail

    def test_claude_without_credentials_says_why(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setattr("gyrfalcon.config.cfg_get", lambda *a, **k: "")

        result = count_text("claude-sonnet-4-5", SAMPLE)
        assert result.method == UNAVAILABLE
        assert "no local tokenizer" in result.detail
        assert "free" in result.detail


@needs_tiktoken
class TestOpenAI:
    def test_counts_exactly(self):
        result = count_text("gpt-4o", SAMPLE)
        assert result.method == EXACT
        assert result.tokens > 50

    def test_different_generations_use_different_vocabularies(self):
        import tiktoken

        assert tiktoken.encoding_for_model("gpt-4o").name == "o200k_base"
        assert tiktoken.encoding_for_model("gpt-3.5-turbo").name == "cl100k_base"

    def test_an_unknown_openai_model_falls_back_to_current_vocabulary(self):
        """A GPT released after this tiktoken build must still count."""
        result = count_text("gpt-7-turbo-preview", SAMPLE)
        assert result.method == EXACT
        assert result.tokenizer == "o200k_base"

    def test_longer_text_costs_more_tokens(self):
        short = count_text("gpt-4o", "hello world")
        assert short.tokens < count_text("gpt-4o", SAMPLE).tokens


@needs_hf
class TestOpenWeight:
    """These hit the HF hub on first use and are cached thereafter."""

    @pytest.mark.parametrize("model", [
        "llama-3-70b", "llama-2-7b", "mistral-large",
        "qwen-2.5-72b", "deepseek-v3", "gemma-2-27b",
    ])
    def test_each_family_counts_exactly(self, model):
        result = count_text(model, SAMPLE)
        assert result.method == EXACT
        assert result.tokens > 50

    def test_ungated_mirrors_are_used_for_gated_families(self):
        """meta-llama and google/gemma 401 without an HF token."""
        assert "NousResearch" in count_text("llama-3-70b", SAMPLE).tokenizer
        assert not count_text("gemma-2-27b", SAMPLE).tokenizer.startswith("google/")

    def test_vocabularies_actually_differ(self):
        """Guard against every family silently resolving to one tokenizer."""
        tokenizers = {count_text(m, SAMPLE).tokenizer
                      for m in ("llama-3-70b", "qwen-2.5-72b", "gemma-2-27b")}
        assert len(tokenizers) == 3


class TestClaudeViaAPI:
    """Claude counts through `count_tokens`, mocked here (no key in CI)."""

    @pytest.fixture
    def fake_client(self, monkeypatch):
        calls = []

        class Messages:
            def count_tokens(self, **kwargs):
                calls.append(kwargs)
                return type("R", (), {"input_tokens": 1234})()

        class Client:
            messages = Messages()

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("gyrfalcon.net.get_anthropic_client",
                            lambda *a, **k: Client())
        reg._count_claude_cached.cache_clear()
        yield calls
        reg._count_claude_cached.cache_clear()

    def test_uses_the_api_and_reports_method_api(self, fake_client):
        result = count_text("claude-sonnet-4-5", SAMPLE)
        assert result.method == API
        assert result.tokens == 1234
        assert result.tokenizer == "anthropic/count_tokens"

    def test_system_and_tools_are_sent_to_the_endpoint(self, fake_client):
        """count_tokens accepts both, which is what makes the estimate exact."""
        tools = [{"name": "read_file", "input_schema": {"type": "object"}}]
        count_request("claude-sonnet-4-5", "hi", system="SYS", tools=tools)

        assert len(fake_client) == 1
        sent = fake_client[0]
        assert sent["system"] == "SYS"
        assert sent["tools"] == tools
        assert sent["model"] == "claude-sonnet-4-5"

    def test_identical_content_is_counted_once(self, fake_client):
        """Counting is free but rate-limited, so repeats must not re-request."""
        for _ in range(4):
            count_text("claude-sonnet-4-5", SAMPLE)
        assert len(fake_client) == 1

    def test_different_content_counts_again(self, fake_client):
        count_text("claude-sonnet-4-5", "one")
        count_text("claude-sonnet-4-5", "two")
        assert len(fake_client) == 2

    def test_a_failing_api_call_degrades_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        def boom(*a, **k):
            raise RuntimeError("429 rate limited")

        monkeypatch.setattr("gyrfalcon.net.get_anthropic_client", boom)
        reg._count_claude_cached.cache_clear()

        result = count_text("claude-sonnet-4-5", SAMPLE)
        assert result.method == UNAVAILABLE
        assert "count_tokens failed" in result.detail


@needs_tiktoken
class TestCountRequest:
    """The whole request, which is what the estimator quotes by default."""

    def test_system_prompt_and_tools_add_tokens(self):
        bare = count_request("gpt-4o", "hi")
        with_overhead = count_request(
            "gpt-4o", "hi",
            system="You are a helpful assistant. " * 50,
            tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
        )
        assert with_overhead.tokens > bare.tokens

    def test_overhead_dominates_a_short_input(self):
        """The reason the toggle defaults on."""
        text = "hi"
        content_only = count_text("gpt-4o", text)
        full = count_request("gpt-4o", text, system="You are helpful. " * 200)
        assert full.tokens > content_only.tokens * 20

    def test_tool_schemas_are_counted_as_serialized_json(self):
        tools = [{"name": "x", "input_schema": {"type": "object",
                                                "properties": {"a": {"type": "string"}}}}]
        counted = count_request("gpt-4o", "", tools=tools)
        raw = count_text("gpt-4o", json.dumps(tools, sort_keys=True))
        assert counted.tokens >= raw.tokens

    def test_framing_is_included_and_flagged(self):
        result = count_request("gpt-4o", "hello", message_count=5)
        assert result.method == EXACT
        assert "framing" in result.detail

    def test_unavailable_model_stays_unavailable(self):
        assert count_request("wat-9000", "hi").method == UNAVAILABLE


class TestDescribeIsCheap:
    """Rendering a model list must not cost network calls."""

    def test_describe_never_calls_count_tokens(self, monkeypatch):
        """Eight Claude models in a picker must not mean eight API requests."""
        calls = []

        def should_not_run(*a, **k):
            calls.append(a)
            raise AssertionError("describe() made a network call")

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("gyrfalcon.net.get_anthropic_client", should_not_run)
        reg._count_claude_cached.cache_clear()

        for model in ("claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"):
            info = describe(model)
            assert info["available"] is True
            assert info["method"] == API
        assert calls == []

    def test_is_available_is_also_cheap(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(
            "gyrfalcon.net.get_anthropic_client",
            lambda *a, **k: pytest.fail("is_available() made a network call"),
        )
        from gyrfalcon.tokenizers import is_available

        assert is_available("claude-sonnet-4-5") is True

    def test_claude_is_reported_unavailable_without_a_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        monkeypatch.setattr("gyrfalcon.config.cfg_get", lambda *a, **k: "")
        info = describe("claude-sonnet-4-5")
        assert info["available"] is False
        assert info["local"] is False
