"""Mailbox poller — a flow that notices new mail and emits an event per message.

Drop this file (or a symlink to it) in `~/.gyrfalcon/flows/` and the `@flow`
below registers itself on import, the same way a skill folder is picked up.
`deploy.py` next to this file then binds it to a 5-minute schedule.

The flow's only job is **detection**: "new mail exists, here are its headers."
What Gyrfalcon *does* about it is a separate concern, wired as an automation on
the `gyrfalcon.email.received` event (see the bottom of this file) — so you can
change the reaction without touching the poller, and the event log keeps a
replayable record of every message ever seen.

Deliberately a poll, not IMAP IDLE: IDLE needs a long-lived socket, a re-IDLE
every ~29 minutes, and its own thread, and thread boundaries are this
codebase's recurring defect class. A poll is stateless, idempotent and
restart-safe. IDLE can be added later behind the same event without any
consumer changing.
"""

from __future__ import annotations

import email
import imaplib
import json
import logging
from dataclasses import dataclass
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any, Optional

from gyrfalcon.config import cfg_get, get_env_value
from gyrfalcon.flow import flow, task
from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home

logger = logging.getLogger("gyrfalcon.flow.email")

#: Header fields worth carrying on the event. Bodies and attachments stay on
#: the server until something actually asks for them — an event payload is not
#: the place to put a 4MB PDF, and it keeps mail out of the LLM context until a
#: tool deliberately fetches it.
HEADER_FIELDS = ("Message-ID", "From", "To", "Subject", "Date")


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MailboxConfig:
    host: str
    port: int
    user: str
    password: str
    use_ssl: bool = True

    @property
    def account_key(self) -> str:
        """Identifies the account in the UID high-water file."""
        # `user` is usually already the full address; don't double the @.
        return self.user if "@" in self.user else f"{self.user}@{self.host}"


def load_mailbox_config() -> Optional[MailboxConfig]:
    """Reads `email:` from config.yaml, with the password from `.env`.

    Returns None when unconfigured, so the flow can no-op cleanly instead of
    failing every 5 minutes on a machine that has never set this up.
    """
    host = cfg_get("email.imap_host")
    user = cfg_get("email.imap_user")
    password = get_env_value("GYRFALCON_IMAP_PASSWORD")
    if not (host and user and password):
        return None
    return MailboxConfig(
        host=host,
        port=int(cfg_get("email.imap_port", 993)),
        user=user,
        password=password,
        use_ssl=bool(cfg_get("email.imap_ssl", True)),
    )


# ── UID high-water mark ──────────────────────────────────────────────────────
#
# One integer per (account, folder). A JSON file under GYRFALCON_HOME rather
# than a flow DB table on purpose: a table would need a forward-only entry in
# `db/migrations.py`, which is a lot of ceremony for one integer.

def _state_path() -> Path:
    d = get_gyrfalcon_home() / "email"
    d.mkdir(exist_ok=True)
    return d / "uid_state.json"


def _read_state() -> dict[str, int]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Unreadable UID state at %s; treating as empty", path)
        return {}


def _write_state(state: dict[str, int]) -> None:
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic — a torn write here would replay the mailbox


def _state_key(cfg: MailboxConfig, folder: str) -> str:
    return f"{cfg.account_key}/{folder}"


# ── Tasks ────────────────────────────────────────────────────────────────────

@task(retries=3, retry_delay_seconds=10)
def fetch_new_messages(
    cfg: MailboxConfig, folder: str, last_uid: int, limit: int
) -> list[dict[str, Any]]:
    """Returns header dicts for messages with UID > `last_uid`, oldest first.

    Retries live on the task, not the flow: a refused connection or a dropped
    socket is the one flaky step here, and retrying it alone is cheaper than
    re-running the whole poll around it.
    """
    opener = imaplib.IMAP4_SSL if cfg.use_ssl else imaplib.IMAP4
    conn = opener(cfg.host, cfg.port)
    try:
        conn.login(cfg.user, cfg.password)
        # readonly: polling must never change \Seen or the folder's state.
        conn.select(folder, readonly=True)

        # UID ranges, never `SINCE <date>`: dates are timezone-fuzzy and
        # re-deliver messages around midnight. UIDs are monotonic per folder.
        typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
        if typ != "OK":
            raise RuntimeError(f"UID SEARCH failed on {folder!r}: {typ}")

        uids = [int(u) for u in data[0].split()]
        # `UID n:*` always returns at least the highest UID even when nothing
        # is newer than `last_uid` — filter it out rather than re-emitting it.
        uids = sorted(u for u in uids if u > last_uid)[:limit]
        if not uids:
            return []

        fields = " ".join(HEADER_FIELDS)
        messages = []
        for uid in uids:
            # PEEK, so fetching does not mark the message read.
            typ, raw = conn.uid("FETCH", str(uid), f"(BODY.PEEK[HEADER.FIELDS ({fields})])")
            if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                logger.warning("Could not fetch headers for UID %s in %s", uid, folder)
                continue
            messages.append(_parse_headers(uid, folder, raw[0][1]))
        return messages
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001 - logout failures must not fail the poll
            pass


def _parse_headers(uid: int, folder: str, raw: bytes) -> dict[str, Any]:
    msg = email.message_from_bytes(raw)

    def header(name: str) -> str:
        value = msg.get(name)
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:  # noqa: BLE001 - a malformed header is not worth failing on
            return value

    message_id = header("Message-ID").strip("<>")
    return {
        "uid": uid,
        "folder": folder,
        # Fall back to the UID so `resource_id` is never empty; some senders
        # genuinely omit Message-ID.
        "message_id": message_id or f"{folder}#{uid}",
        "from": header("From"),
        "to": header("To"),
        "subject": header("Subject"),
        "date": header("Date"),
    }


@task
def emit_received(messages: list[dict[str, Any]], account: str) -> int:
    """Emits one `gyrfalcon.email.received` per message. Returns the count.

    `resource_id` is the Message-ID, so dedupe and `follows_chain` work without
    any extra bookkeeping.
    """
    from gyrfalcon.flow.events import emit

    for msg in messages:
        emit(
            "gyrfalcon.email.received",
            resource_id=msg["message_id"],
            payload={**msg, "account": account},
        )
    return len(messages)


# ── The flow ─────────────────────────────────────────────────────────────────

@flow(name="poll_mailbox", timeout_seconds=120)
def poll_mailbox(folder: str = "INBOX", limit: int = 50) -> dict[str, Any]:
    """Poll one IMAP folder and emit an event per new message.

    `limit` caps a single tick: the first run against an old mailbox would
    otherwise emit thousands of events at once. The high-water mark advances by
    at most `limit` per tick, so a backlog drains over several runs instead.
    """
    cfg = load_mailbox_config()
    if cfg is None:
        logger.info("Mailbox not configured (email.imap_host / GYRFALCON_IMAP_PASSWORD); skipping.")
        return {"status": "unconfigured", "new_messages": 0}

    state = _read_state()
    key = _state_key(cfg, folder)
    last_uid = int(state.get(key, 0))

    messages = fetch_new_messages(cfg, folder, last_uid, limit)
    if not messages:
        return {"status": "ok", "new_messages": 0, "last_uid": last_uid}

    emitted = emit_received(messages, cfg.account_key)

    # Advance only after the events are durable. Crashing before this replays
    # the batch on the next tick — at-least-once, which the Message-ID
    # `resource_id` makes safe, and which is the right side to err on.
    new_last_uid = max(m["uid"] for m in messages)
    state[key] = new_last_uid
    _write_state(state)

    return {
        "status": "ok",
        "new_messages": emitted,
        "last_uid": new_last_uid,
        "subjects": [m["subject"] for m in messages],
    }


# ── Reacting to the event ────────────────────────────────────────────────────
#
# Detection above is complete on its own. This is the other half — "whenever X,
# do Y" — kept here as a commented example because the right action is a
# product decision (notify? open a session? file it?), not something a sample
# should pick for you. Register it once at process start, not at import:
#
#     from gyrfalcon.flow.automations import Trigger, get_automation_engine
#
#     def on_mail(hits, event):
#         print(f"New mail from {event.payload['from']}: {event.payload['subject']}")
#
#     get_automation_engine().register(
#         name="notify-on-mail",
#         trigger=Trigger(event_pattern="gyrfalcon.email.received"),
#         action=on_mail,
#     )
#
# `automations.action_run_deployment("triage-mail")` is the other useful shape:
# a second flow — an agent step, say — reacting to the event, rather than an
# inline callback.
