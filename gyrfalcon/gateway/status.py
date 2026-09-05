"""Gateway status — health checks, metrics, and diagnostics."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("status")



@dataclass
class GatewayMetrics:
    """Runtime metrics for the gateway."""
    start_time: float = field(default_factory=time.time)
    total_messages_in: int = 0
    total_messages_out: int = 0
    total_errors: int = 0
    total_sessions_created: int = 0
    total_sessions_expired: int = 0
    active_sessions: int = 0
    # Per-platform counters
    platform_messages: dict[str, int] = field(default_factory=dict)
    # Per-model counters
    model_calls: dict[str, int] = field(default_factory=dict)
    # Latency tracking (last 100 request durations in ms)
    _latencies: list[float] = field(default_factory=list)

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def avg_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        return sum(self._latencies) / len(self._latencies)

    @property
    def p95_latency_ms(self) -> float:
        if not self._latencies:
            return 0.0
        sorted_l = sorted(self._latencies)
        idx = int(len(sorted_l) * 0.95)
        return sorted_l[min(idx, len(sorted_l) - 1)]

    def record_message_in(self, platform: str) -> None:
        logger.debug("Beginning of record_message_in")
        self.total_messages_in += 1
        self.platform_messages[platform] = self.platform_messages.get(platform, 0) + 1

    def record_message_out(self) -> None:
        logger.debug("Beginning of record_message_out")
        self.total_messages_out += 1

    def record_error(self) -> None:
        logger.debug("Beginning of record_error")
        self.total_errors += 1

    def record_latency(self, duration_ms: float) -> None:
        logger.debug("Beginning of record_latency")
        self._latencies.append(duration_ms)
        if len(self._latencies) > 100:
            self._latencies = self._latencies[-100:]

    def record_model_call(self, model: str) -> None:
        logger.debug("Beginning of record_model_call")
        self.model_calls[model] = self.model_calls.get(model, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        logger.debug("Beginning of to_dict")
        return {
            "uptime_seconds": round(self.uptime_seconds, 1),
            "total_messages_in": self.total_messages_in,
            "total_messages_out": self.total_messages_out,
            "total_errors": self.total_errors,
            "active_sessions": self.active_sessions,
            "total_sessions_created": self.total_sessions_created,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
            "platform_messages": self.platform_messages,
            "model_calls": self.model_calls,
        }


@dataclass
class HealthStatus:
    """Gateway health status."""
    healthy: bool = True
    checks: dict[str, bool] = field(default_factory=dict)
    message: str = "OK"

    def to_dict(self) -> dict[str, Any]:
        logger.debug("Beginning of to_dict")
        return {
            "healthy": self.healthy,
            "checks": self.checks,
            "message": self.message,
        }


class GatewayStatus:
    """Manages gateway health and metrics."""

    def __init__(self) -> None:
        self.metrics = GatewayMetrics()
        self._health_checks: dict[str, callable] = {}

    def register_health_check(self, name: str, check_fn: callable) -> None:
        """Register a health check function that returns bool."""
        logger.debug("Beginning of register_health_check")
        self._health_checks[name] = check_fn

    def get_health(self) -> HealthStatus:
        """Run all health checks and return status."""
        logger.debug("Beginning of get_health")
        status = HealthStatus()
        for name, check_fn in self._health_checks.items():
            try:
                result = check_fn()
                status.checks[name] = result
                if not result:
                    status.healthy = False
                    status.message = f"Check failed: {name}"
            except Exception as e:
                status.checks[name] = False
                status.healthy = False
                status.message = f"Check error ({name}): {e}"
        return status

    def get_metrics(self) -> dict[str, Any]:
        """Get current metrics as dict."""
        logger.debug("Beginning of get_metrics")
        return self.metrics.to_dict()
