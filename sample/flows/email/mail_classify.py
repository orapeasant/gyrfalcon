"""Thread resolution, classification, and flow dispatch for stored email.

This is the "what Gyrfalcon does about it" half that `mailbox_flow.py`'s
module docstring says is a separate concern from detection. It is kept in its
own module for the same reason: you can change classification rules or the
label->flow map without touching the poller or the IMAP-fetch code at all.

Pipeline, per message (see `ingest_message`):

1. **Thread resolution** — `In-Reply-To` / `References` headers point at the
   Message-ID of the mail being replied to or forwarded. If either resolves
   to an email already in `emails`, the new row inherits that email's
   `thread_id` (the thread's root), so a whole reply/forward chain collapses
   onto one `thread_id` no matter how deep it goes. A message with neither
   header, or whose parent isn't in the database, starts a new thread rooted
   at itself.
2. **Store** — one row in `emails`, keyed by Message-ID (idempotent: a
   re-delivered message with the same id is a no-op via INSERT OR IGNORE).
3. **Classify** — `classify_email()` returns zero or more string labels.
   Pluggable: the default is a small keyword/subject rule set so this module
   has no hard LLM dependency, but a real deployment can supply its own
   classifier (see `classify_with`).
4. **Tag** — one row per label in `email_classifications`.
5. **Dispatch** — for each label, look up `classification_flow_map`. If a
   row exists, is enabled, and names a flow that is actually registered,
   run it (via the flow registry, `run_definition_now`) with the email's id
   and thread id as parameters. One label can only ever map to one flow —
   that is the table's primary key — but one email with N labels fans out to
   up to N flow runs. Every attempt (dispatched, skipped, or errored) is
   recorded in `email_dispatches` so "why didn't flow X run for this email"
   is always answerable from the database, not from logs.
"""

from __future__ import annotations

import json
import logging
import re
import time
from email.utils import getaddresses
from typing import Any, Callable, Optional

from mail_db import MailDB, get_mail_db, new_id

logger = logging.getLogger("gyrfalcon.flow.email.classify")

Classifier = Callable[[dict[str, Any]], list[tuple[str, float]]]


# ── Thread resolution ────────────────────────────────────────────────────

def _split_references(raw: str) -> list[str]:
    # References is a whitespace-separated list of <msgid> tokens, oldest
    # first. strip() the angle brackets the same way mailbox_flow._parse_headers
    # already does for Message-ID, so lookups compare like with like.
    return [tok.strip("<>") for tok in raw.split() if tok.strip("<>")]


def resolve_thread_id(db: MailDB, message_id: str, in_reply_to: str,
                       references: list[str]) -> str:
    """Returns the thread root's id for a new message.

    Checked in the order a mail client would actually give us signal:
    In-Reply-To is the direct parent; References is the whole ancestor
    chain, oldest-first, so its *last* entry is still the direct parent even
    when In-Reply-To is missing (some forwards drop it). Falling through to
    "start a new thread rooted at yourself" is correct, not a failure: the
    parent may simply predate this mailbox being polled.
    """
    candidates = []
    if in_reply_to:
        candidates.append(in_reply_to.strip("<>"))
    candidates.extend(reversed(references))  # nearest ancestor first

    seen = set()
    for parent_id in candidates:
        if not parent_id or parent_id in seen:
            continue
        seen.add(parent_id)
        row = db.fetchone("SELECT thread_id FROM emails WHERE id = ?", (parent_id,))
        if row is not None:
            return row["thread_id"]
    return message_id  # no known parent: this message is its own thread root


def classify_direction(in_reply_to: str, references: list[str], subject: str) -> str:
    if in_reply_to or references:
        subj = (subject or "").strip().lower()
        if subj.startswith("fwd:") or subj.startswith("fw:"):
            return "forward"
        return "reply"
    return "inbound"


# ── Storage ───────────────────────────────────────────────────────────────

def store_message(db: MailDB, msg: dict[str, Any]) -> tuple[str, bool]:
    """Inserts one email row. Returns (email_id, inserted).

    `inserted=False` means the Message-ID was already present — the same
    at-least-once delivery `poll_mailbox` already tolerates for events, kept
    tolerant here too so a re-polled UID never produces a duplicate thread
    entry or a double classification.
    """
    message_id = msg["message_id"]
    in_reply_to = (msg.get("in_reply_to") or "").strip()
    references = _split_references(msg.get("references") or "")
    thread_id = resolve_thread_id(db, message_id, in_reply_to, references)
    direction = classify_direction(in_reply_to, references, msg.get("subject", ""))

    cur = db.execute(
        "INSERT OR IGNORE INTO emails "
        "(id, account, folder, uid, thread_id, in_reply_to, references_ids, "
        " from_addr, to_addr, subject, date_header, body_text, raw_headers, "
        " direction, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            message_id, msg.get("account", ""), msg.get("folder", ""),
            msg.get("uid"), thread_id, in_reply_to, json.dumps(references),
            msg.get("from", ""), msg.get("to", ""), msg.get("subject", ""),
            msg.get("date", ""), msg.get("body_text", ""),
            json.dumps({k: msg.get(k, "") for k in
                        ("from", "to", "subject", "date")}),
            direction, time.time(),
        ),
    )
    inserted = cur.rowcount > 0
    return message_id, inserted


# ── Classification ───────────────────────────────────────────────────────

#: Default rules: (label, compiled pattern over "subject\nbody"). Intended as
#: a working placeholder, not a product decision — swap in an LLM or a real
#: rules engine via `classify_with` without touching the pipeline.
_DEFAULT_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("invoice", re.compile(r"\b(invoice|payment due|bill(ed)?)\b", re.I)),
    ("urgent", re.compile(r"\b(urgent|asap|immediately|action required)\b", re.I)),
    ("meeting_request", re.compile(r"\b(schedule|calendar invite|meeting|call) \b|"
                                    r"\bmeet(ing)? (at|on)\b", re.I)),
    ("support_request", re.compile(r"\b(issue|problem|not working|error|bug|help)\b", re.I)),
    ("newsletter", re.compile(r"\b(unsubscribe|newsletter|digest)\b", re.I)),
)


def default_classify(msg: dict[str, Any]) -> list[tuple[str, float]]:
    text = f"{msg.get('subject', '')}\n{msg.get('body_text', '')}"
    hits = []
    for label, pattern in _DEFAULT_RULES:
        if pattern.search(text):
            hits.append((label, 1.0))
    return hits


_active_classifier: Classifier = default_classify


def classify_with(fn: Classifier) -> None:
    """Swap the classifier used by `classify_email` (e.g. an LLM-backed one).

    Deliberately a plain module-level swap, not a plugin registry: this
    pipeline has exactly one classifier active at a time, and the flow file
    that wants a smarter one just calls this before `poll_mailbox` runs.
    """
    global _active_classifier
    _active_classifier = fn


def classify_email(db: MailDB, email_id: str, msg: dict[str, Any],
                    source: str = "rule") -> list[str]:
    """Runs the active classifier and tags `email_id` with every label found.

    Idempotent per (email_id, label) via INSERT OR IGNORE on the primary
    key, so reclassifying an already-tagged email is safe.
    """
    hits = _active_classifier(msg)
    now = time.time()
    labels = []
    for label, confidence in hits:
        db.execute(
            "INSERT OR IGNORE INTO email_classifications "
            "(email_id, label, confidence, source, created_at) VALUES (?,?,?,?,?)",
            (email_id, label, confidence, source, now),
        )
        labels.append(label)
    return labels


# ── Classification -> flow map ───────────────────────────────────────────

def set_flow_mapping(db: MailDB, label: str, flow_name: str,
                      enabled: bool = True, parameters: Optional[dict] = None) -> None:
    now = time.time()
    params_json = json.dumps(parameters or {})
    db.execute(
        "INSERT INTO classification_flow_map (label, flow_name, enabled, parameters, "
        " created_at, updated_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(label) DO UPDATE SET flow_name=excluded.flow_name, "
        " enabled=excluded.enabled, parameters=excluded.parameters, "
        " updated_at=excluded.updated_at",
        (label, flow_name, int(enabled), params_json, now, now),
    )


def get_flow_mapping(db: MailDB, label: str) -> Optional[dict[str, Any]]:
    row = db.fetchone(
        "SELECT * FROM classification_flow_map WHERE label = ?", (label,)
    )
    return dict(row) if row else None


def list_flow_mappings(db: MailDB) -> list[dict[str, Any]]:
    return [dict(r) for r in db.fetchall(
        "SELECT * FROM classification_flow_map ORDER BY label"
    )]


# ── Dispatch ──────────────────────────────────────────────────────────────

def _record_dispatch(db: MailDB, email_id: str, label: str, flow_name: str,
                      run_id: Optional[str], status: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO email_dispatches (id, email_id, label, flow_name, run_id, "
        " status, detail, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (new_id(), email_id, label, flow_name, run_id, status, detail, time.time()),
    )


def dispatch_for_labels(db: MailDB, email_id: str, thread_id: str,
                         labels: list[str]) -> list[dict[str, Any]]:
    """Invokes the mapped flow for every label. Returns one result dict per
    label attempted, whether or not the flow actually ran.

    A missing mapping or a disabled mapping is `skipped`, not an error — an
    unmapped label is an expected, common state (e.g. "newsletter" with
    nothing wired up yet), and treating it as a failure would make the
    dispatch log useless as a signal.
    """
    from gyrfalcon.flow.registry import get_definition, run_definition_now

    results = []
    for label in labels:
        mapping = get_flow_mapping(db, label)
        if mapping is None:
            _record_dispatch(db, email_id, label, "", None, "skipped",
                              "no classification_flow_map entry")
            results.append({"label": label, "status": "skipped",
                             "reason": "unmapped"})
            continue
        if not mapping["enabled"]:
            _record_dispatch(db, email_id, label, mapping["flow_name"], None,
                              "skipped", "mapping disabled")
            results.append({"label": label, "status": "skipped",
                             "reason": "disabled"})
            continue

        flow_name = mapping["flow_name"]
        if get_definition(flow_name) is None:
            _record_dispatch(db, email_id, label, flow_name, None, "error",
                              "flow not registered")
            results.append({"label": label, "status": "error",
                             "reason": "flow not registered"})
            continue

        params = json.loads(mapping["parameters"] or "{}")
        params = {**params, "email_id": email_id, "thread_id": thread_id,
                  "label": label}
        try:
            run_id = run_definition_now(flow_name, params)
            _record_dispatch(db, email_id, label, flow_name, run_id, "dispatched")
            results.append({"label": label, "status": "dispatched",
                             "flow_name": flow_name, "run_id": run_id})
        except Exception as e:  # noqa: BLE001 - one bad flow must not block others
            logger.warning("Dispatch of %r for email %s failed", flow_name,
                            email_id, exc_info=True)
            _record_dispatch(db, email_id, label, flow_name, None, "error", str(e))
            results.append({"label": label, "status": "error", "reason": str(e)})
    return results


# ── The whole per-message pipeline ───────────────────────────────────────

def ingest_message(msg: dict[str, Any], db: Optional[MailDB] = None) -> dict[str, Any]:
    """Store, classify, and dispatch one message. The unit `poll_mailbox`
    calls once per fetched message."""
    db = db or get_mail_db()
    email_id, inserted = store_message(db, msg)
    if not inserted:
        return {"email_id": email_id, "status": "duplicate"}

    row = db.fetchone("SELECT thread_id FROM emails WHERE id = ?", (email_id,))
    thread_id = row["thread_id"] if row else email_id

    labels = classify_email(db, email_id, msg)
    dispatches = dispatch_for_labels(db, email_id, thread_id, labels)
    return {
        "email_id": email_id,
        "thread_id": thread_id,
        "status": "ingested",
        "labels": labels,
        "dispatches": dispatches,
    }
