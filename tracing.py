import logging
import os
import time
import traceback
from functools import wraps

from opentelemetry import trace
from opentelemetry.baggage import get_baggage, set_baggage
from opentelemetry.context import attach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.processor.baggage import BaggageSpanProcessor, ALLOW_ALL_BAGGAGE_KEYS
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from pythonjsonlogger import jsonlogger
from logging_loki import LokiHandler
from prometheus_client import Counter, Histogram, Gauge


# --- Tracer ---

tracer_provider = TracerProvider()
tracer_provider.add_span_processor(BaggageSpanProcessor(ALLOW_ALL_BAGGAGE_KEYS))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)

# --- Prometheus Metrics ---

REQUEST_COUNT = Counter(
    "app_requests_total",
    "Total number of requests",
    ["span_name", "status"],
)

REQUEST_DURATION = Histogram(
    "app_request_duration_seconds",
    "Request duration in seconds",
    ["span_name"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

HTTP_REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently in progress",
    ["method", "path"],
)




# --- Filters ---

class TraceContextFilter(logging.Filter):
    """Auto-injects trace_id, span_id, user_id, org_id into every log record."""

    def filter(self, record):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        record.trace_id = format(ctx.trace_id, "032x")
        record.span_id = format(ctx.span_id, "016x")
        record.user_id = get_baggage("user_id") or ""
        record.org_id = get_baggage("org_id") or ""
        record.form_record_id = get_baggage("form_record_id") or ""
        record.form_id = get_baggage("form_id") or ""
        return True


class ErrorExtractionFilter(logging.Filter):
    """Extracts error details into structured JSON for ERROR+ logs."""

    @staticmethod
    def _clean_trace(entries):
        cleaned = []
        for entry in entries:
            lines = entry.split('\n')
            filtered = [l for l in lines if not (l.strip() and all(c == '^' for c in l.strip()))]
            cleaned.append('\n'.join(filtered).rstrip())
        return cleaned

    def filter(self, record):
        if record.levelno >= logging.ERROR and record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            frames = traceback.extract_tb(exc_tb)

            app_frames = [f for f in frames if "/app/" in f.filename]
            if app_frames:
                location = f"{app_frames[-1].filename}:{app_frames[-1].lineno} in {app_frames[-1].name}"
            elif frames:
                last_frame = frames[-1]
                location = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}"
            else:
                location = ""

            message = str(exc_value) if exc_value else ""
            if not message and exc_value and exc_value.__cause__:
                message = str(exc_value.__cause__)
            if hasattr(exc_value, 'request') and hasattr(exc_value.request, 'url'):
                url = str(exc_value.request.url)
                message = f"{message} (url={url})" if message else f"request to {url} failed"

            full_trace = self._clean_trace(traceback.format_exception(exc_type, exc_value, exc_tb))
            app_trace = self._clean_trace(traceback.format_list(app_frames) if app_frames else [])

            record.error = {
                "type": exc_type.__name__ if exc_type else "",
                "message": message,
                "location": location,
                "stack_trace": app_trace,
                "stack_trace_full": full_trace,
            }
            record.exc_info = None
            record.exc_text = None
        else:
            record.error = None
        return True


# --- Setup helper ---

def instrument_app(app):
    """Instrument a FastAPI app with OpenTelemetry, excluding health/metrics."""
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")


def setup(name="app", level=logging.INFO, propagate=False):
    """Logger setup: instrumentors + Loki handler + filters."""
    HTTPXClientInstrumentor().instrument()

    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"levelname": "level", "asctime": "timestamp"},
    )

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = propagate

    loki_url = os.getenv("LOKI_URL")
    if loki_url and not logger.handlers:
        loki_handler = LokiHandler(
            url=f"{loki_url}/loki/api/v1/push",
            tags={"service_name": "observability-api-1"},
            version="1",
        )
        loki_handler.setFormatter(formatter)
        logger.addHandler(loki_handler)

    has_trace_filter = any(isinstance(f, TraceContextFilter) for f in logger.filters)
    if not has_trace_filter:
        logger.addFilter(TraceContextFilter())

    return logger


# --- Span metadata logging ---

def log_span(span, span_name: str, logger=None, duration_ms=None, trace_id=None, span_id=None, **extra_attrs):
    """Logs span completion metadata (duration, status, attributes)."""
    if logger is None:
        logger = logging.getLogger("app")

    HTTP_ATTRS = {"http.method", "http.url", "http.route", "http.status_code"}
    attributes = {k: v for k, v in span.attributes.items() if k in HTTP_ATTRS} if hasattr(span, 'attributes') else {}
    attributes.update(extra_attrs)

    if duration_ms is None:
        if span.start_time and span.end_time and span.end_time > span.start_time:
            duration_ms = round((span.end_time - span.start_time) / 1e6, 2)
        else:
            duration_ms = 0

    if trace_id is None:
        ctx = span.get_span_context()
        trace_id = format(ctx.trace_id, "032x")
    if span_id is None:
        ctx = span.get_span_context()
        span_id = format(ctx.span_id, "016x")

    logger.info(
        "span_completed",
        extra={
            "span_name": span_name,
            "duration_ms": duration_ms,
            "span_status": str(span.status.status_code.name),
            "trace_id": trace_id,
            "span_id": span_id,
            "attributes": attributes,
        },
    )


# --- Span enrichment ---

BAGGAGE_FIELDS = ["user_id", "org_id", "form_id", "form_record_id"]

def enrich_span_from_context(span, request):
    """Reads baggage with request.state fallback, sets span attributes, re-attaches baggage."""
    resolved = {}
    for field in BAGGAGE_FIELDS:
        value = get_baggage(field) or getattr(request.state, field, '') or ""
        resolved[field] = value
        if span.is_recording():
            span.set_attribute(field, value)

    if any(resolved.values()):
        ctx = None
        for k, v in resolved.items():
            ctx = set_baggage(k, v, context=ctx)
        attach(ctx)

    return resolved


# --- Decorator ---

def traced(span_name, skip_paths=None):
    """Decorator: creates a span, sets status, logs span metadata on completion.
    span_name can be a string or a callable(*args, **kwargs) -> str.
    skip_paths: optional set of paths to skip tracing for (checks Request arg)."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if skip_paths:
                req = args[0] if args else kwargs.get("request")
                if req and hasattr(req, "url") and req.url.path in skip_paths:
                    return await func(*args, **kwargs)

            name = span_name(*args, **kwargs) if callable(span_name) else span_name
            with tracer.start_as_current_span(name) as span:
                # BaggageSpanProcessor automatically adds baggage as span attributes
                # user_id = get_baggage("user_id") or ""
                # org_id = get_baggage("org_id") or ""
                # span.set_attribute("user_id", user_id)
                # span.set_attribute("org_id", org_id)

                try:
                    result = await func(*args, **kwargs)
                    span.set_status(trace.StatusCode.OK)
                    REQUEST_COUNT.labels(span_name=name, status="ok").inc()
                    return result
                except Exception:
                    span.set_status(trace.StatusCode.ERROR)
                    REQUEST_COUNT.labels(span_name=name, status="error").inc()
                    raise
                finally:
                    ctx = span.get_span_context()
                    trace_id = format(ctx.trace_id, "032x")
                    span_id = format(ctx.span_id, "016x")
                    if span.start_time:
                        duration_ms = round((time.time() - span.start_time / 1e9) * 1000, 2)
                    else:
                        duration_ms = 0
                    log_span(span, name, duration_ms=duration_ms, trace_id=trace_id, span_id=span_id)
                    REQUEST_DURATION.labels(span_name=name).observe(duration_ms / 1000)
        return wrapper
    return decorator

