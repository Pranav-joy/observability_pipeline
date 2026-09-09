import logging
import os
import time
import traceback
from functools import wraps

from opentelemetry import trace
from opentelemetry.baggage import get_baggage
from opentelemetry.sdk.trace import TracerProvider
from pythonjsonlogger import jsonlogger
from logging_loki import LokiHandler
from prometheus_client import Counter, Histogram

# --- Tracer ---

trace.set_tracer_provider(TracerProvider())
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

    def filter(self, record):
        if record.levelno >= logging.ERROR and record.exc_info:
            exc_type, exc_value, exc_tb = record.exc_info
            frames = traceback.extract_tb(exc_tb)
            if frames:
                last_frame = frames[-1]
                location = f"{last_frame.filename}:{last_frame.lineno} in {last_frame.name}"
            else:
                location = ""
            record.error = {
                "type": exc_type.__name__ if exc_type else "",
                "message": str(exc_value) if exc_value else "",
                "location": location,
                "stack_trace": "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
            }
            record.exc_info = None
            record.exc_text = None
        else:
            record.error = None
        return True


# --- Setup helper ---

def setup(name="app", level=logging.INFO, propagate=False):
    """Logger setup: Loki handler + filters only. Console output handled by dictConfig."""
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
            tags={"application": name, "service_name": "observability-api-1"},
            version="1",
        )
        loki_handler.setFormatter(formatter)
        logger.addHandler(loki_handler)

    has_trace_filter = any(isinstance(f, TraceContextFilter) for f in logger.filters)
    has_error_filter = any(isinstance(f, ErrorExtractionFilter) for f in logger.filters)
    if not has_trace_filter:
        logger.addFilter(TraceContextFilter())
    if not has_error_filter:
        logger.addFilter(ErrorExtractionFilter())

    return logger


# --- Span metadata logging ---

def log_span(span, span_name: str, logger=None, duration_ms=None, trace_id=None, span_id=None, **extra_attrs):
    """Logs span completion metadata (duration, status, attributes)."""
    if logger is None:
        logger = logging.getLogger("app")

    attributes = {
        "user_id": get_baggage("user_id") or "",
        "org_id": get_baggage("org_id") or "",
    }
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
                user_id = get_baggage("user_id") or ""
                org_id = get_baggage("org_id") or ""
                span.set_attribute("user_id", user_id)
                span.set_attribute("org_id", org_id)

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
