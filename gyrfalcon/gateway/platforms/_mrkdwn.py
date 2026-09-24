"""Slack text conversion — Markdown out, Slack markup in, and message splitting.

Slack's `mrkdwn` is not Markdown, and the differences show on every message the
agent sends: `**bold**` is `*bold*`, headings and tables do not exist, links are
`<url|label>`, and a code fence's language tag is printed as a literal first
line. Getting this wrong makes every reply look broken.

It is also a security boundary. Slack parses `<!channel>`, `<!here>` and
`<@U123>` out of message text, so model output containing them — and model
output can be steered by anyone who can put text in front of the model — would
page an entire workspace. Every `<`, `>` and `&` in body text is therefore
escaped; the only angle brackets in what we send are the link forms this module
builds itself.
"""

from __future__ import annotations

import re

#: Slack allows 40,000 characters per message but truncates and reflows long
#: ones badly; 3,800 keeps each chunk comfortably inside a single text block.
DEFAULT_LIMIT = 3800

# Private-use characters as markers: cannot occur in real text, survive regexes.
_BOLD_OPEN, _BOLD_CLOSE = "\ue000", "\ue001"
_HOLD = "\ue002"

# A language tag counts only when a newline follows it: ```x = 1``` is code.
_FENCE = re.compile(r"```(?:[A-Za-z0-9_+.#-]*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap_tables(text: str) -> str:
    """Slack has no tables. A block of `|` rows with a separator becomes a code
    block, which at least keeps the columns aligned in a monospace font."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEPARATOR.match(lines[i + 1]):
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            out.append("```")
            out.extend(row for k, row in enumerate(lines[i:j]) if k != 1)
            out.append("```")
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def markdown_to_mrkdwn(text: str) -> str:
    """Convert model Markdown to Slack mrkdwn, escaping everything Slack would parse."""
    if not text:
        return text

    # The markers below park code and links while the rest is reformatted. If
    # the input already contains them it could pull parked text back out — or
    # name an index that does not exist and crash delivery. They are not real
    # text, so they go.
    text = re.sub("[\ue000-\ue002]", "", text)

    text = _wrap_tables(text)

    held: list[str] = []

    def hold(replacement: str) -> str:
        held.append(replacement)
        return f"{_HOLD}{len(held) - 1}{_HOLD}"

    # Code is protected first: nothing inside it may be reformatted, but it must
    # still be escaped, and its language tag dropped (Slack prints it).
    text = _FENCE.sub(lambda m: hold("```\n" + _escape(m.group(1)).rstrip("\n") + "\n```"), text)
    text = _INLINE_CODE.sub(lambda m: hold("`" + _escape(m.group(1)) + "`"), text)

    # Links are built before escaping so their brackets survive; the parts
    # inside are escaped individually.
    def link(m: re.Match) -> str:
        label, url = m.group(1), m.group(2).strip()
        if not re.match(r"(?i)(https?://|mailto:)", url):
            # `javascript:` and friends are not worth a link; show them as text.
            return hold(f"{_escape(label)} ({_escape(url)})")
        return hold(f"<{_escape(url).replace('|', '%7C')}|{_escape(label).replace('|', chr(0xA6))}>")

    text = re.sub(r"\[([^\]\n]+)\]\(([^)\s]+)\)", link, text)

    text = _escape(text)
    # A leading `>` is a block quote in Slack too — the one angle bracket we want back.
    text = re.sub(r"(?m)^(\s*)&gt; ?", r"\1> ", text)

    text = re.sub(
        r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$",
        lambda m: _BOLD_OPEN + m.group(1).strip("*_ ") + _BOLD_CLOSE,
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", _BOLD_OPEN + r"\1" + _BOLD_CLOSE, text)
    text = re.sub(r"__(.+?)__", _BOLD_OPEN + r"\1" + _BOLD_CLOSE, text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])", r"_\1_", text)
    text = text.replace(_BOLD_OPEN, "*").replace(_BOLD_CLOSE, "*")
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # Rules before bullets: `* * *` is a rule, not a list of asterisks.
    text = re.sub(r"(?m)^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$", "\u2500" * 12, text)
    text = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", "\\1\u2022 ", text)

    def unhold(m: re.Match) -> str:
        return held[int(m.group(1))]

    # Held segments can contain no markers of their own, so one pass restores all.
    return re.sub(f"{_HOLD}(\\d+){_HOLD}", unhold, text)


def split_message(text: str, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Split on paragraph, then line, then word boundaries, keeping fences balanced.

    A chunk that ends inside a code block is closed and the next one reopened,
    so a long block renders as several code blocks rather than as one that
    swallows everything after it.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ") if cut != limit else rest[cut:]
    if rest.strip():
        chunks.append(rest)

    balanced: list[str] = []
    reopen = False
    for chunk in chunks:
        if reopen:
            chunk = "```\n" + chunk
        reopen = chunk.count("```") % 2 == 1
        if reopen:
            chunk += "\n```"
        balanced.append(chunk)
    return balanced


_MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_REF = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL = re.compile(r"<!([a-z]+)(?:\^[^>|]*)?(?:\|([^>]*))?>")
_LINK = re.compile(r"<((?:https?|mailto|tel):[^>|]+)(?:\|([^>]*))?>")


def strip_bot_mention(text: str, bot_user_id: str) -> str:
    """Remove the leading `<@BOT>` that addresses the message to us."""
    return re.sub(rf"^\s*<@{re.escape(bot_user_id)}(?:\|[^>]*)?>[\s,:]*", "", text)


def slack_to_plain(text: str) -> str:
    """Slack's wire markup to readable text the model can reason about."""
    if not text:
        return text
    def link(m: re.Match) -> str:
        url, label = m.group(1), m.group(2)
        return f"{label} ({url})" if label and label != url else url

    text = _LINK.sub(link, text)
    text = _CHANNEL_REF.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = _MENTION.sub(lambda m: f"@{m.group(1)}", text)
    text = _SPECIAL.sub(lambda m: f"@{m.group(2) or m.group(1)}", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
