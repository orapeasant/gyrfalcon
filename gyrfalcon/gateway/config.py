"""Gateway configuration — one typed view of `gateway:` in config.yaml.

Spec 18-slack.md D1. This replaces two things that used to disagree: a
`GatewayConfig` in `gateway/__init__.py` that read `config.yaml` as an untyped
dict, and a richer one here that read a `gateway.yaml` nothing ever wrote. There
is now exactly one, and it reads the file the dashboard's config editor and
`gyrfalcon setup` already edit.

Access control lives here as data and is enforced in one place — the runner —
so that no adapter can forget to. With `identity.enabled` off (the default) the
allowlist is the *only* thing deciding who may talk to the agent, so its
defaults fail closed: an empty list admits nobody.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("gateway.config")

DEFAULT_MAX_ITERATIONS = 25
DEFAULT_RATE_LIMIT_RPM = 30

#: Chat types that are a private conversation with the bot. Everything else
#: (channels, groups, multi-person DMs) is a shared space.
DIRECT_CHAT_TYPES = frozenset({"dm", "direct", "im", "private"})


def _str_list(value: Any) -> list[str]:
    """Normalise a config value to a list of non-empty strings.

    YAML happily turns an unquoted `U01ABC` list item into a string and a bare
    number into an int; both must compare equal to the string ids adapters
    receive. A scalar where a list was expected is treated as a one-item list
    rather than iterated character by character.
    """
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


@dataclass(frozen=True)
class AllowList:
    """Who may reach the agent through a platform. Default-deny.

    `users` is required for everyone: an empty list admits nobody, not
    everybody — a bot installed into a workspace with no allowlist must refuse,
    not accept. `channels` additionally gates shared spaces: a direct message
    needs only an allowed user, but a channel message needs an allowed user *and*
    an allowed channel, so adding the bot to a channel does not by itself open
    it to whoever is in that channel's user list.
    """

    users: frozenset[str] = frozenset()
    channels: frozenset[str] = frozenset()
    #: Users who may additionally use the tools that change the machine, each
    #: call still going through approval (§6.1, D3). A subset of `users`:
    #: elevation is extra permission for someone already admitted, never a way
    #: in, so listing somebody here and nowhere else grants nothing.
    elevated: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, raw: Any) -> "AllowList":
        raw = raw if isinstance(raw, dict) else {}
        users = frozenset(_str_list(raw.get("users")))
        return cls(
            users=users,
            channels=frozenset(_str_list(raw.get("channels"))),
            elevated=frozenset(_str_list(raw.get("elevated"))) & users,
        )

    def is_elevated(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self.elevated

    def permits(self, user_id: str, chat_id: str, chat_type: str = "") -> bool:
        if not user_id or user_id not in self.users:
            return False
        if chat_type.lower() in DIRECT_CHAT_TYPES:
            return True
        return chat_id in self.channels


@dataclass(frozen=True)
class RoutingRule:
    """Per-conversation overrides. First matching rule wins.

    `pattern` is a regex searched against each of the conversation's names —
    `#general` style channel name, bare channel name, and channel id — so
    `^#ops` and `^C0123` both work. A rule that does not compile is dropped
    with a warning rather than taking the whole gateway down.
    """

    pattern: str
    toolset: Optional[str] = None
    model: Optional[str] = None
    max_iterations: Optional[int] = None
    _compiled: Any = field(default=None, repr=False, compare=False)

    def matches(self, candidates: list[str]) -> bool:
        return any(self._compiled.search(c) for c in candidates if c)


@dataclass
class PlatformConfig:
    """One `gateway.platforms.<name>` block.

    Only what every platform shares is typed here. Anything adapter-specific
    (`mode`, token secret names, `stream`, …) stays in `extra` for the adapter
    to interpret, so adding a platform never means editing this class.
    """

    name: str
    enabled: bool = True
    toolset: Optional[str] = None
    #: Toolset for users in `allow.elevated`. Defaults to the full core set,
    #: which is only reachable at all because every dangerous call in it now has
    #: to be approved by a person.
    elevated_toolset: Optional[str] = None
    max_iterations: Optional[int] = None
    rate_limit_rpm: int = DEFAULT_RATE_LIMIT_RPM
    allow: AllowList = field(default_factory=AllowList)
    #: Reply once to a refused user, rather than staying silent. Off by default:
    #: an answer confirms to a stranger that something is listening.
    reply_on_deny: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    @classmethod
    def from_config(cls, name: str, raw: Any) -> "PlatformConfig":
        raw = raw if isinstance(raw, dict) else {}
        known = {"enabled", "toolset", "max_iterations", "rate_limit_rpm", "allow", "reply_on_deny",
                 "elevated_toolset"}
        return cls(
            name=name,
            enabled=bool(raw.get("enabled", True)),
            toolset=raw.get("toolset") or None,
            elevated_toolset=raw.get("elevated_toolset") or None,
            max_iterations=_positive_int(raw.get("max_iterations")),
            rate_limit_rpm=_positive_int(raw.get("rate_limit_rpm")) or DEFAULT_RATE_LIMIT_RPM,
            allow=AllowList.from_config(raw.get("allow")),
            reply_on_deny=bool(raw.get("reply_on_deny", False)),
            extra={k: v for k, v in raw.items() if k not in known},
        )


def _positive_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass
class GatewayConfig:
    """The whole `gateway:` section."""

    platforms: dict[str, PlatformConfig] = field(default_factory=dict)
    routing_rules: list[RoutingRule] = field(default_factory=list)
    agent_cache_size: int = 128
    idle_ttl: int = 3600
    max_concurrent_sessions: int = 16

    @classmethod
    def from_dict(cls, raw: Any) -> "GatewayConfig":
        raw = raw if isinstance(raw, dict) else {}
        platforms = {
            str(name): PlatformConfig.from_config(str(name), block)
            for name, block in (raw.get("platforms") or {}).items()
        }
        rules: list[RoutingRule] = []
        for entry in raw.get("routing_rules") or []:
            if not isinstance(entry, dict) or not entry.get("pattern"):
                logger.warning("Ignoring routing rule without a pattern: %r", entry)
                continue
            try:
                compiled = re.compile(str(entry["pattern"]))
            except re.error as exc:
                logger.warning("Ignoring routing rule with invalid pattern %r: %s", entry["pattern"], exc)
                continue
            rules.append(RoutingRule(
                pattern=str(entry["pattern"]),
                toolset=entry.get("toolset") or None,
                model=entry.get("model") or None,
                max_iterations=_positive_int(entry.get("max_iterations")),
                _compiled=compiled,
            ))
        return cls(
            platforms=platforms,
            routing_rules=rules,
            agent_cache_size=_positive_int(raw.get("agent_cache_size")) or 128,
            idle_ttl=_positive_int(raw.get("idle_ttl")) or 3600,
            max_concurrent_sessions=_positive_int(raw.get("max_concurrent_sessions")) or 16,
        )

    @classmethod
    def load(cls) -> "GatewayConfig":
        from gyrfalcon.config import load_config

        return cls.from_dict(load_config().get("gateway", {}))

    def route(self, candidates: list[str]) -> Optional[RoutingRule]:
        """The first rule matching any of the conversation's names, if any."""
        for rule in self.routing_rules:
            if rule.matches(candidates):
                return rule
        return None
