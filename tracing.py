import json
import logging
import os
import sys
from functools import wraps

from opentelemetry import trace, metrics
from opentelemetry.baggage import get_baggage, set_baggage
from opentelemetry.context import attach
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk.resources import Resource
from opentelemetry.processor.baggage import BaggageSpanProcessor, BaggageLogProcessor, ALLOW_ALL_BAGGAGE_KEYS
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor


# --- Telemetry setup ---

def init_telemetry(service_name="observability-api-1"):
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create({"service.name": service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)))
    tracer_provider.add_span_processor(BaggageSpanProcessor(ALLOW_ALL_BAGGAGE_KEYS))
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True)))
    logger_provider.add_log_record_processor(BaggageLogProcessor(ALLOW_ALL_BAGGAGE_KEYS))
    set_logger_provider(logger_provider)

    otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)

    meter_provider = MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True), export_interval_millis=15000),
    ])
    metrics.set_meter_provider(meter_provider)

    HTTPXClientInstrumentor().instrument()
    PymongoInstrumentor().instrument(capture_statement=True)
    # sets global logger to use the Otel logging handler
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(otel_handler)

    return otel_handler


# --- Console logging ---

PAYLOAD_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload.json")
DEFAULT_PAYLOAD = ["timestamp", "level", "logger", "message", "exception"]


def _load_payload():
    try:
        with open(PAYLOAD_PATH) as f:
            fields = json.load(f)
        if isinstance(fields, list) and fields:
            return [str(field) for field in fields]
    except (OSError, ValueError):
        pass
    return list(DEFAULT_PAYLOAD)


class JsonFormatter(logging.Formatter):
    def __init__(self, payload=None):
        super().__init__()
        self.payload = payload if payload is not None else _load_payload()

    def format(self, record):
        out = {}
        for key in self.payload:
            if key == "timestamp":
                out[key] = self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%03dZ")
            elif key == "level":
                out[key] = record.levelname
            elif key == "logger":
                out[key] = record.name
            elif key == "message":
                out[key] = record.getMessage()
            elif key == "exception":
                if record.exc_info and record.exc_info[1] is not None:
                    out[key] = self.formatException(record.exc_info)
            elif hasattr(record, key):
                out[key] = getattr(record, key)
        return json.dumps(out, default=str)


def patch_uvicorn_console():
    formatter = JsonFormatter()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.propagate = False


# --- Tracer ---

tracer = trace.get_tracer(__name__)


# --- Baggage fields ---

BAGGAGE_FIELDS = ["user_id", "org_id", "form_id", "form_record_id"]


def rebuild_baggage_from_request(request, fields=None):
    ctx = None
    for key in fields if fields is not None else BAGGAGE_FIELDS:
        val = get_baggage(key) or getattr(request.state, key, None)
        if val:
            val = str(val)
            ctx = set_baggage(key, val) if ctx is None else set_baggage(key, val, context=ctx)
    if ctx:
        attach(ctx)


# --- Decorator ---

def traced(span_name, skip_paths=None):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if skip_paths:
                req = args[0] if args else kwargs.get("request")
                if req and hasattr(req, "url") and req.url.path in skip_paths:
                    return await func(*args, **kwargs)

            name = span_name(*args, **kwargs) if callable(span_name) else span_name
            with tracer.start_as_current_span(name):
                return await func(*args, **kwargs)
        return wrapper
    return decorator
