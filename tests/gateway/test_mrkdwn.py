"""Markdown → Slack mrkdwn, message splitting, and inbound Slack markup."""

from __future__ import annotations

import pytest

from gyrfalcon.gateway.platforms._mrkdwn import (
    DEFAULT_LIMIT,
    slack_to_plain,
    split_message,
    strip_bot_mention,
)
from gyrfalcon.gateway.platforms._mrkdwn import (
    markdown_to_mrkdwn as md,
)


class TestNothingSlackWouldParseSurvives:
    """Slack turns `<!channel>` and `<@U…>` in message text into real pings.
    Model output can be steered by whoever can put text in front of the model."""

    @pytest.mark.parametrize("text", [
        "<!channel> deploy is done",
        "<!here>",
        "<!everyone>",
        "<@U0123ABCD> look at this",
        "<!subteam^S123|@oncall>",
        "hello <#C123|general>",
        "<http://evil.example|click me>",
    ])
    def test_special_markup_is_neutralised(self, text):
        out = md(text)
        assert "<" not in out.replace("&lt;", "") and ">" not in out.replace("&gt;", "")
        assert "&lt;" in out

    def test_the_only_angle_brackets_are_links_we_built(self):
        out = md("see [docs](https://x.io) and <!channel>")
        assert out.count("<") == 1 and out.startswith("see <https://x.io|docs>")
        assert "&lt;!channel&gt;" in out

    def test_ampersands_are_escaped_so_entities_cannot_be_forged(self):
        assert md("a &lt;!channel&gt; b") == "a &amp;lt;!channel&amp;gt; b"

    def test_a_non_web_link_scheme_is_shown_not_linked(self):
        out = md("[click](javascript:alert(1))")
        assert "<" not in out and "click" in out

    def test_pipes_in_urls_and_labels_cannot_break_out_of_the_link(self):
        out = md("[a|b](https://x.io/p|q)")
        assert out == "<https://x.io/p%7Cq|a¦b>"

    def test_code_is_escaped_too(self):
        assert md("```\n<!channel>\n```") == "```\n&lt;!channel&gt;\n```"
        assert md("run `<!here>`") == "run `&lt;!here&gt;`"


class TestFormatting:
    def test_bold_italic_strike(self):
        assert md("**bold** and *it* and ~~gone~~") == "*bold* and _it_ and ~gone~"

    def test_double_underscore_is_bold(self):
        assert md("__bold__") == "*bold*"

    def test_bold_is_not_then_turned_into_italics(self):
        assert md("**a** *b*") == "*a* _b_"

    def test_headings_become_bold_and_keep_the_paragraph_break(self):
        assert md("# Title\n\n- a\n- b") == "*Title*\n\n• a\n• b"

    def test_heading_with_inner_emphasis_and_closing_hashes(self):
        assert md("## **Big** ##\ntext") == "*Big*\ntext"

    @pytest.mark.parametrize("marker", ["-", "*", "+"])
    def test_bullets(self, marker):
        assert md(f"{marker} one\n{marker} two") == "• one\n• two"

    def test_nested_bullets_keep_indent(self):
        assert md("- a\n  - b") == "• a\n  • b"

    def test_ordered_lists_are_left_alone(self):
        assert md("1. one\n2. two") == "1. one\n2. two"

    def test_blockquote_survives(self):
        assert md("> quoted\ntext") == "> quoted\ntext"

    def test_a_horizontal_rule_is_not_a_list_of_asterisks(self):
        assert md("* * *") == "─" * 12
        assert md("a\n\n---\n\nb") == "a\n\n" + "─" * 12 + "\n\nb"

    def test_arithmetic_is_not_italics(self):
        assert md("2 * 3 * 4") == "2 * 3 * 4"
        assert md("a*b*c") == "a*b*c"

    def test_plain_text_and_bare_urls_pass_through(self):
        assert md("just words, https://example.com/x?a=1") == "just words, https://example.com/x?a=1"

    def test_empty_input(self):
        assert md("") == "" and md(None) is None

    def test_a_link_label_is_not_reformatted(self):
        assert md("[**hi**](https://x.io)") == "<https://x.io|**hi**>"


class TestCode:
    def test_the_language_tag_is_dropped_because_slack_prints_it(self):
        assert md("```python\nx = 1\n```") == "```\nx = 1\n```"

    def test_nothing_inside_code_is_reformatted(self):
        assert md("```\n**not bold** # not a heading\n- not a bullet\n```") == \
            "```\n**not bold** # not a heading\n- not a bullet\n```"
        assert md("`**x**`") == "`**x**`"

    def test_a_fence_with_no_language_or_trailing_newline(self):
        assert md("```x = 1```") == "```\nx = 1\n```"

    def test_text_around_code_is_still_formatted(self):
        assert md("**a**\n```\nx\n```\n**b**") == "*a*\n```\nx\n```\n*b*"

    def test_placeholders_cannot_be_forged_by_the_input(self):
        # The converter parks code behind private-use markers; text containing
        # the same characters must not be able to pull other text back out...
        out = md("`secret`" + "\ue002" + "0" + "\ue002")
        assert out.count("secret") == 1

    def test_a_forged_out_of_range_placeholder_cannot_crash_delivery(self):
        # ...or name an index that does not exist (IndexError inside send()).
        assert "\ue002" not in md("a \ue002999\ue002 b `x`")
        assert md("\ue000\ue001\ue002") == ""

    def test_a_language_tag_needs_a_newline_after_it(self):
        assert md("```python x = 1```") == "```\npython x = 1\n```"


class TestTables:
    def test_a_table_becomes_a_code_block_without_the_separator_row(self):
        out = md("| a | b |\n|---|---|\n| 1 | 2 |")
        assert out == "```\n| a | b |\n| 1 | 2 |\n```"

    def test_aligned_separators(self):
        assert md("| a |\n|:--:|\n| 1 |").startswith("```")

    def test_a_lone_pipe_line_is_not_a_table(self):
        assert md("| just a pipe") == "| just a pipe"


class TestSplitting:
    def test_short_text_is_one_chunk(self):
        assert split_message("hello") == ["hello"]

    def test_every_chunk_fits(self):
        text = ("word " * 200 + "\n\n") * 20
        chunks = split_message(text, limit=500)
        assert len(chunks) > 5 and all(len(c) <= 500 + 4 for c in chunks)

    def test_it_prefers_paragraph_boundaries(self):
        text = "a" * 30 + "\n\n" + "b" * 30
        assert split_message(text, limit=40) == ["a" * 30, "b" * 30]

    def test_no_content_is_lost(self):
        text = " ".join(f"w{i}" for i in range(2000))
        rebuilt = " ".join(split_message(text, limit=300))
        assert rebuilt.split() == text.split()

    def test_an_unbroken_string_is_hard_cut_not_dropped(self):
        chunks = split_message("x" * 1000, limit=300)
        assert "".join(chunks) == "x" * 1000 and all(len(c) <= 300 for c in chunks)

    def test_a_code_block_split_across_chunks_stays_balanced(self):
        body = "\n".join(f"line {i}" for i in range(300))
        chunks = split_message(f"intro\n```\n{body}\n```\noutro", limit=400)
        assert len(chunks) > 3
        for c in chunks:
            assert c.count("```") % 2 == 0, f"unbalanced fence in: {c[:60]!r}"

    def test_default_limit_is_below_slacks_reflow_threshold(self):
        assert DEFAULT_LIMIT < 4000

    def test_a_converted_long_reply_never_exceeds_the_limit(self):
        chunks = split_message(md("**x** " * 3000))
        assert all(len(c) <= DEFAULT_LIMIT + 4 for c in chunks)


class TestInbound:
    def test_the_leading_bot_mention_is_stripped(self):
        assert strip_bot_mention("<@UBOT123> what's up", "UBOT123") == "what's up"
        assert strip_bot_mention("<@UBOT123>, hi", "UBOT123") == "hi"
        assert strip_bot_mention("<@UBOT123|bot> hi", "UBOT123") == "hi"

    def test_only_our_own_mention_is_stripped(self):
        assert strip_bot_mention("<@UOTHER> hi", "UBOT123") == "<@UOTHER> hi"
        assert strip_bot_mention("hey <@UBOT123> hi", "UBOT123") == "hey <@UBOT123> hi"

    def test_a_control_command_is_exposed_once_the_mention_is_gone(self):
        from gyrfalcon.gateway.run import parse_control
        assert parse_control(strip_bot_mention("<@UBOT123> !stop", "UBOT123")) == ("stop", "")

    def test_links_channels_mentions_and_entities_become_readable(self):
        out = slack_to_plain("hi <@U123> see <https://x.io|docs> in <#C1|ops> &lt;ok&gt; &amp; <!here>")
        assert out == "hi @U123 see docs (https://x.io) in #ops <ok> & @here"

    def test_a_bare_link_is_unwrapped(self):
        assert slack_to_plain("<https://x.io/a>") == "https://x.io/a"
        assert slack_to_plain("<https://x.io|https://x.io>") == "https://x.io"

    def test_special_mentions_keep_their_label(self):
        assert slack_to_plain("<!subteam^S1|@oncall> <!channel>") == "@@oncall @channel"

    def test_a_channel_without_a_name_falls_back_to_its_id(self):
        assert slack_to_plain("<#C123>") == "#C123"

    def test_empty(self):
        assert slack_to_plain("") == ""

    def test_round_trip_never_reintroduces_a_ping(self):
        out = md(slack_to_plain("<!channel> <@U1>"))
        assert "<" not in out.replace("&lt;", "")
