"""OpenTelemetry instrumentation for Gyrfalcon.

Captures:
- WebSocket chat communication (messages, deltas, completions)
- HTTP API requests
- Agent conversation turns and tool calls
- LLM provider interactions
"""

from __future__ import annotations

import os
from functools import wraps
from typing import Any, Callable, Optional
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv.resource import ResourceAttributes
from opentelemetry.trace import Status, StatusCode, Span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("telemetry")

# Global tracer instance
_tracer: Optional[trace.Tracer] = None
_initialized = False
_noop_tracer: Optional[trace.Tracer] = None


def telemetry_enabled() -> bool:
    """Whether tracing is on. Config-driven so the hot paths can skip span work."""
    from gyrfalcon.config import cfg_get
    return bool(cfg_get("performance.telemetry_enabled", False))


def init_telemetry(
    service_name: str = "gyrfalcon",
    otlp_endpoint: str | None = None,
    console_export: bool = False,
) -> trace.Tracer:
    """Initialize OpenTelemetry tracing.
    
    Args:
        service_name: Name of the service for resource identification
        otlp_endpoint: OTLP exporter endpoint (e.g., "http://localhost:4317")
                      Falls back to OTEL_EXPORTER_OTLP_ENDPOINT env var
        console_export: If True, also export spans to console (for debugging)
    
    Returns:
        Configured tracer instance
    """
    logger.debug("Beginning of init_telemetry")
    global _tracer, _initialized
    
    if _initialized:
        return _tracer
    
    # Create resource with service info
    resource = Resource.create({
        ResourceAttributes.SERVICE_NAME: service_name,
        ResourceAttributes.SERVICE_VERSION: "0.1.0",
        "deployment.environment": os.getenv("GYRFALCON_ENV", "development"),
    })
    
    # Create tracer provider
    provider = TracerProvider(resource=resource)
    
    # Add OTLP exporter if endpoint configured
    endpoint = otlp_endpoint or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            otlp_exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
            logger.info(f"OTLP exporter configured: {endpoint}")
        except Exception as e:
            logger.warning(f"Failed to configure OTLP exporter: {e}")
    
    # Add console exporter for debugging
    if console_export or os.getenv("OTEL_CONSOLE_EXPORT", "").lower() == "true":
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("Console span exporter enabled")
    
    # Set as global provider
    trace.set_tracer_provider(provider)
    
    _tracer = trace.get_tracer("gyrfalcon", "0.1.0")
    _initialized = True
    
    logger.info("OpenTelemetry initialized")
    return _tracer


def get_tracer() -> trace.Tracer:
    """Get the global tracer, initializing if needed.

    Returns a no-op tracer when telemetry is disabled — spans are then
    non-recording, so callers on hot paths pay almost nothing.
    """
    logger.debug("Beginning of get_tracer")
    global _tracer, _noop_tracer
    if not telemetry_enabled():
        if _noop_tracer is None:
            _noop_tracer = trace.NoOpTracer()
        return _noop_tracer
    if not _tracer:
        init_telemetry()
    return _tracer


@contextmanager
def trace_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
):
    """Context manager for creating traced spans.
    
    Usage:
        with trace_span("process_message", {"message.type": "user"}) as span:
            # ... do work ...
            span.set_attribute("result.tokens", 150)
    """
    logger.debug("Beginning of trace_span")
    tracer = get_tracer()
    with tracer.start_as_current_span(name, kind=kind) as span:
        if attributes:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        try:
            yield span
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise


def trace_function(
    name: str | None = None,
    attributes: dict[str, Any] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
):
    """Decorator to trace function calls.
    
    Usage:
        @trace_function("agent.run_turn")
        def run_turn(self, message: str):
            ...
    """
    logger.debug("Beginning of trace_function")
    def decorator(func: Callable) -> Callable:
        logger.debug("Beginning of decorator")
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        
        @wraps(func)
        def wrapper(*args, **kwargs):
            logger.debug("Beginning of wrapper")
            with trace_span(span_name, attributes, kind) as span:
                # Add function args as attributes (non-sensitive only)
                if args and hasattr(args[0], '__class__'):
                    span.set_attribute("class", args[0].__class__.__name__)
                return func(*args, **kwargs)
        
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            with trace_span(span_name, attributes, kind) as span:
                if args and hasattr(args[0], '__class__'):
                    span.set_attribute("class", args[0].__class__.__name__)
                return await func(*args, **kwargs)
        
        import asyncio
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return wrapper
    
    return decorator


# --- WebSocket Communication Tracing ---

class WebSocketTracer:
    """Traces WebSocket communication for the chat interface."""
    
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.tracer = get_tracer()
        self._current_turn_span: Optional[Span] = None
        self._message_count = 0
    
    def trace_message_received(self, method: str, params: dict) -> Span:
        """Trace an incoming WebSocket message."""
        logger.debug("Beginning of trace_message_received")
        span = self.tracer.start_span(
            f"ws.receive.{method}",
            kind=trace.SpanKind.SERVER,
        )
        span.set_attribute("ws.method", method)
        span.set_attribute("ws.session_id", self.session_id or "unknown")
        span.set_attribute("ws.message_index", self._message_count)
        self._message_count += 1
        
        # Add non-sensitive params
        if "message" in params:
            span.set_attribute("ws.message_length", len(params["message"]))
        
        return span
    
    def trace_message_sent(self, method: str, params: dict) -> Span:
        """Trace an outgoing WebSocket message."""
        logger.debug("Beginning of trace_message_sent")
        span = self.tracer.start_span(
            f"ws.send.{method}",
            kind=trace.SpanKind.CLIENT,
        )
        span.set_attribute("ws.method", method)
        span.set_attribute("ws.session_id", self.session_id or "unknown")
        
        # Track streaming deltas
        if method == "message.delta":
            span.set_attribute("ws.delta_length", len(params.get("content", "")))
        elif method == "message.complete":
            span.set_attribute("ws.response_length", len(params.get("content", "")))
        
        return span
    
    def start_turn_span(self, user_message: str) -> Span:
        """Start a span for a complete conversation turn."""
        logger.debug("Beginning of start_turn_span")
        self._current_turn_span = self.tracer.start_span(
            "chat.turn",
            kind=trace.SpanKind.SERVER,
        )
        self._current_turn_span.set_attribute("chat.session_id", self.session_id or "unknown")
        self._current_turn_span.set_attribute("chat.user_message_length", len(user_message))
        return self._current_turn_span
    
    def end_turn_span(self, response_length: int = 0, error: str | None = None):
        """End the current turn span."""
        logger.debug("Beginning of end_turn_span")
        if self._current_turn_span:
            self._current_turn_span.set_attribute("chat.response_length", response_length)
            if error:
                self._current_turn_span.set_status(Status(StatusCode.ERROR, error))
            else:
                self._current_turn_span.set_status(Status(StatusCode.OK))
            self._current_turn_span.end()
            self._current_turn_span = None


# --- Agent Tracing ---

class AgentTracer:
    """Traces agent operations including LLM calls and tool executions."""
    
    def __init__(self, session_id: str, model: str):
        self.session_id = session_id
        self.model = model
        self.tracer = get_tracer()
        self._turn_count = 0
    
    @contextmanager
    def trace_conversation_turn(self, user_message: str):
        """Trace a complete conversation turn."""
        logger.debug("Beginning of trace_conversation_turn")
        self._turn_count += 1
        with self.tracer.start_as_current_span(
            "agent.conversation_turn",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("agent.session_id", self.session_id)
            span.set_attribute("agent.model", self.model)
            span.set_attribute("agent.turn_number", self._turn_count)
            span.set_attribute("agent.user_message_length", len(user_message))
            yield span
    
    @contextmanager
    def trace_llm_call(self, model: str, message_count: int):
        """Trace an LLM API call."""
        logger.debug("Beginning of trace_llm_call")
        with self.tracer.start_as_current_span(
            "agent.llm_call",
            kind=trace.SpanKind.CLIENT,
        ) as span:
            span.set_attribute("llm.model", model)
            span.set_attribute("llm.message_count", message_count)
            yield span
    
    @contextmanager
    def trace_tool_call(self, tool_name: str, args: dict):
        """Trace a tool execution."""
        logger.debug("Beginning of trace_tool_call")
        with self.tracer.start_as_current_span(
            f"agent.tool.{tool_name}",
            kind=trace.SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("tool.name", tool_name)
            # Add safe arg info (avoid logging sensitive data)
            span.set_attribute("tool.arg_count", len(args))
            if "path" in args:
                span.set_attribute("tool.path", str(args["path"])[:200])
            yield span
    
    def record_token_usage(self, span: Span, input_tokens: int, output_tokens: int):
        """Record token usage on a span."""
        logger.debug("Beginning of record_token_usage")
        span.set_attribute("llm.usage.input_tokens", input_tokens)
        span.set_attribute("llm.usage.output_tokens", output_tokens)
        span.set_attribute("llm.usage.total_tokens", input_tokens + output_tokens)
    
    def record_streaming_metrics(self, span: Span, delta_count: int, total_chars: int):
        """Record streaming response metrics."""
        logger.debug("Beginning of record_streaming_metrics")
        span.set_attribute("llm.streaming.delta_count", delta_count)
        span.set_attribute("llm.streaming.total_chars", total_chars)


# --- FastAPI Instrumentation Helper ---

def instrument_fastapi(app):
    """Instrument a FastAPI application with OpenTelemetry.
    
    Call this after creating your FastAPI app:
        app = FastAPI()
        instrument_fastapi(app)
    """
    logger.debug("Beginning of instrument_fastapi")
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        logger.info("FastAPI instrumented with OpenTelemetry")
    except ImportError:
        logger.warning("FastAPI instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument FastAPI: {e}")


def instrument_httpx():
    """Instrument httpx for outgoing HTTP request tracing."""
    logger.debug("Beginning of instrument_httpx")
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        logger.info("HTTPX instrumented with OpenTelemetry")
    except ImportError:
        logger.warning("HTTPX instrumentation not available")
    except Exception as e:
        logger.warning(f"Failed to instrument HTTPX: {e}")
