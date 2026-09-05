"""Gateway configuration — platform credentials and routing rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from gyrfalcon.gyrfalcon_constants import get_gyrfalcon_home
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("config")



@dataclass
class PlatformCredentials:
    """Credentials for a specific platform."""
    platform: str
    token: str = ""
    api_key: str = ""
    webhook_secret: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class RoutingRule:
    """Routing rule for incoming messages."""
    pattern: str  # regex or glob
    target_model: str = ""
    target_toolset: str = "default"
    max_iterations: int = 25
    system_prompt_override: str = ""


@dataclass
class GatewayConfig:
    """Full gateway configuration."""
    host: str = "0.0.0.0"
    port: int = 9121
    max_concurrent_sessions: int = 50
    session_ttl_hours: int = 24
    platforms: dict[str, PlatformCredentials] = field(default_factory=dict)
    routing_rules: list[RoutingRule] = field(default_factory=list)
    allowed_origins: list[str] = field(default_factory=lambda: ["*"])
    rate_limit_rpm: int = 60
    rate_limit_burst: int = 10

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "GatewayConfig":
        """Load gateway config from YAML file."""
        logger.debug("Beginning of load")
        if path is None:
            path = get_gyrfalcon_home() / "gateway.yaml"
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text()) or {}
            config = cls(
                host=data.get("host", "0.0.0.0"),
                port=data.get("port", 9121),
                max_concurrent_sessions=data.get("max_concurrent_sessions", 50),
                session_ttl_hours=data.get("session_ttl_hours", 24),
                allowed_origins=data.get("allowed_origins", ["*"]),
                rate_limit_rpm=data.get("rate_limit_rpm", 60),
                rate_limit_burst=data.get("rate_limit_burst", 10),
            )
            # Load platform credentials
            for name, cred_data in data.get("platforms", {}).items():
                config.platforms[name] = PlatformCredentials(
                    platform=name,
                    token=cred_data.get("token", ""),
                    api_key=cred_data.get("api_key", ""),
                    webhook_secret=cred_data.get("webhook_secret", ""),
                    extra={k: v for k, v in cred_data.items()
                           if k not in ("token", "api_key", "webhook_secret")},
                )
            # Load routing rules
            for rule_data in data.get("routing_rules", []):
                config.routing_rules.append(RoutingRule(
                    pattern=rule_data.get("pattern", ".*"),
                    target_model=rule_data.get("model", ""),
                    target_toolset=rule_data.get("toolset", "default"),
                    max_iterations=rule_data.get("max_iterations", 25),
                    system_prompt_override=rule_data.get("system_prompt", ""),
                ))
            return config
        except (OSError, yaml.YAMLError):
            return cls()

    def save(self, path: Optional[Path] = None) -> None:
        """Save gateway config to YAML."""
        logger.debug("Beginning of save")
        if path is None:
            path = get_gyrfalcon_home() / "gateway.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "session_ttl_hours": self.session_ttl_hours,
            "allowed_origins": self.allowed_origins,
            "rate_limit_rpm": self.rate_limit_rpm,
            "rate_limit_burst": self.rate_limit_burst,
        }
        if self.platforms:
            data["platforms"] = {}
            for name, cred in self.platforms.items():
                data["platforms"][name] = {
                    "token": cred.token,
                    "api_key": cred.api_key,
                    "webhook_secret": cred.webhook_secret,
                    **cred.extra,
                }
        if self.routing_rules:
            data["routing_rules"] = [
                {
                    "pattern": r.pattern,
                    "model": r.target_model,
                    "toolset": r.target_toolset,
                    "max_iterations": r.max_iterations,
                    "system_prompt": r.system_prompt_override,
                }
                for r in self.routing_rules
            ]
        path.write_text(yaml.dump(data, default_flow_style=False))
