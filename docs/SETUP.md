# Observability Setup Guide

A complete guide for integrating structured logging, distributed tracing, and Prometheus metrics into any FastAPI application using this observability stack.

---

## Table of Contents

1. [What This Stack Provides](#1-what-this-stack-provides)
2. [Architecture Overview](#2-architecture-overview)
3. [Prerequisites](#3-prerequisites)
4. [File Inventory](#4-file-inventory)
5. [Step-by-Step Integration](#5-step-by-step-integration)
   - 5.1 [Copy Files to Your Project](#51-copy-files-to-your-project)
   - 5.2 [Install Dependencies](#52-install-dependencies)
   - 5.3 [Configure Environment Variables](#53-configure-environment-variables)
   - 5.4 [Modify tracing.py for Your Service](#54-modify-tracingpy-for-your-service)
   - 5.5 [Integrate into Your FastAPI App](#55-integrate-into-your-fastapi-app)
   - 5.6 [Add the Tracing Middleware](#56-add-the-tracing-middleware)
   - 5.7 [Set Up Baggage in Auth/Endpoints](#57-set-up-baggage-in-authendpoints)
   - 5.8 [Add the Metrics Endpoint](#58-add-the-metrics-endpoint)
   - 5.9 [Configure Structured Logging](#59-configure-structured-logging)
6. [Grafana Dashboard Setup](#6-grafana-dashboard-setup)
   - 6.1 [Import Dashboards](#61-import-dashboards)
   - 6.2 [Update Datasource UIDs](#62-update-datasource-uids)
   - 6.3 [Update Service Name in Queries](#63-update-service-name-in-queries)
   - 6.4 [File-Based Provisioning (Optional)](#64-file-based-provisioning-optional)
7. [Configuration Reference](#7-configuration-reference)
8. [How It All Works Together](#8-how-it-all-works-together)
9. [Troubleshooting](#9-troubleshooting)
10. [FAQ](#10-faq)

---

## 1. What This Stack Provides

| Pillar | What You Get | Backend |
|--------|-------------|---------|
| **Logs** | Structured JSON logs with `trace_id`, `span_id`, `user_id`, `org_id` injected into every log line. Error stack traces extracted into structured fields. | Loki |
| **Metrics** | Request rate (`app_requests_total`), request duration histogram (`app_request_duration_seconds`) with per-endpoint labels. | Prometheus |
| **Trace Context** | Every log line is correlated to its trace via `trace_id`/`span_id`. Baggage fields (`user_id`, `org_id`) flow through the entire request lifecycle. | OpenTelemetry SDK |

**What you do NOT need to change:** Your existing business logic. The observability layer is additive — it wraps around your endpoints without modifying them.

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                     Your FastAPI App                     │
│                                                         │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ tracing.py  │  │ middleware    │  │ your endpoints│  │
│  │             │  │              │  │               │  │
│  │ - Tracer    │──│ - span attrs │──│ - set_baggage│  │
│  │ - Logger    │  │ - log_span() │  │ - @traced    │  │
│  │ - Filters   │  │ - enrich()   │  │               │  │
│  └──────┬──────┘  └──────┬───────┘  └───────────────┘  │
│         │                │                              │
└─────────┼────────────────┼──────────────────────────────┘
          │                │
          ▼                ▼
   ┌──────────────┐  ┌──────────────┐
   │    Loki      │  │  Prometheus  │
   │ (structured  │  │  (counters,  │
   │  JSON logs)  │  │  histograms) │
   └──────┬───────┘  └──────┬───────┘
          │                  │
          ▼                  ▼
   ┌─────────────────────────────────┐
   │           Grafana               │
   │  - Log Volume panel (Loki)      │
   │  - Logs panel (Loki)            │
   │  - Request Rate panel (Prom)    │
   │  - Request Duration panel (Prom)│
   └─────────────────────────────────┘
```

**Data flow:**

1. Request arrives → FastAPI auto-instrumentor creates a span
2. Middleware sets HTTP attributes (`method`, `url`, `route`) on the span
3. Your endpoint code sets baggage (`user_id`, `org_id`, etc.)
4. `BaggageSpanProcessor` automatically copies baggage → span attributes
5. `TraceContextFilter` injects `trace_id`/`span_id`/baggage into every `logger.info()` call
6. `LokiHandler` pushes structured JSON logs to Loki
7. Middleware logs span completion via `log_span()` → another structured log entry
8. Prometheus scrapes `/metrics` endpoint for request rate/duration

---

## 3. Prerequisites

Before integrating, you need:

- **Python 3.10+** (for your FastAPI app)
- **Loki instance** — accessible from your app (e.g., `http://loki:3100` or a Grafana Cloud Loki endpoint)
- **Prometheus instance** — accessible from your app, with scrape config pointing at your app's `/metrics` endpoint
- **Grafana instance** — with Loki and Prometheus already configured as datasources

**Optional (not required for logs + metrics):**
- **Tempo** — for distributed trace visualization and service graphs. Without Tempo, trace context still flows through logs but you don't get trace timelines in Grafana.

---

## 4. File Inventory

These are the files you need to copy or reference from this repository:

| File | Purpose | Copy? | Modify? |
|------|---------|-------|---------|
| `tracing.py` | Core observability library: tracer setup, instrumentors, logging filters, span helpers, `@traced` decorator | **Yes** | **Yes** — service name, path filter |
| `logging_config.json` | Python `dictConfig` for structured JSON logging to stderr/stdout | **Yes** | Maybe — adjust logger names |
| `requirements.txt` | Python dependencies (reference for what to add) | Reference | Add relevant lines to your own |
| `grafana/provisioning/dashboards/json/internal_dashboard.json` | Grafana v2 log viewer dashboard | **Yes** (for Grafana) | **Yes** — service name, datasource UID |
| `grafana/provisioning/dashboards/json/internal_dashboard_wrapped.json` | Same as above with wrapped log lines | **Yes** (for Grafana) | **Yes** — service name, datasource UID |
| `grafana/provisioning/dashboards/disabled/observability-api.json` | Full v1 dashboard: Log Volume + Logs + Trace Search + Request Rate + Duration | **Yes** (for Grafana) | **Yes** — service name, datasource UIDs |

**Files you do NOT need:**
- `main.py` — this is the prototype app, not needed for your integration
- `db.py` — prototype database layer
- `docker-compose.yml` — prototype infrastructure
- `alloy/`, `loki/`, `tempo/`, `prometheus/`, `grafana/provisioning/datasources/` — prototype infrastructure configs

---

## 5. Step-by-Step Integration

### 5.1 Copy Files to Your Project

Copy these two files to the root of your FastAPI project:

```
your-project/
├── your_app/
│   ├── __init__.py
│   ├── main.py          # your existing FastAPI app
│   └── ...
├── tracing.py            # <-- COPY THIS
├── logging_config.json   # <-- COPY THIS
├── requirements.txt      # <-- ADD DEPENDENCIES
└── ...
```

### 5.2 Install Dependencies

Add these lines to your `requirements.txt`:

```txt
# Observability stack
opentelemetry-api
opentelemetry-sdk
opentelemetry-instrumentation-fastapi
opentelemetry-instrumentation-httpx
opentelemetry-exporter-otlp-proto-grpc
opentelemetry-processor-baggage
python-json-logger
python-logging-loki
prometheus-client
```

Then install:

```bash
pip install -r requirements.txt
```

**What each package does:**

| Package | Purpose |
|---------|---------|
| `opentelemetry-api` | OpenTelemetry API — trace context propagation, span creation |
| `opentelemetry-sdk` | OpenTelemetry SDK — `TracerProvider`, span processors |
| `opentelemetry-instrumentation-fastapi` | Auto-instruments FastAPI — creates spans for every HTTP request |
| `opentelemetry-instrumentation-httpx` | Auto-instruments HTTPX — creates child spans for outbound HTTP calls |
| `opentelemetry-exporter-otlp-proto-grpc` | Exports traces via OTLP gRPC (to Tempo or any OTLP-compatible backend) |
| `opentelemetry-processor-baggage` | `BaggageSpanProcessor` — automatically copies baggage fields into span attributes |
| `python-json-logger` | JSON log formatter — makes log lines structured and queryable in Loki |
| `python-logging-loki` | `LokiHandler` — pushes log records to Loki's push API |
| `prometheus-client` | Prometheus metrics — `Counter`, `Histogram` for request rate/duration |

### 5.3 Configure Environment Variables

Set these environment variables in your deployment (Docker, Kubernetes, systemd, etc.):

| Variable | Required | Example | Purpose |
|----------|----------|---------|---------|
| `LOKI_URL` | Yes | `http://loki:3100` | Loki push endpoint. Logs are sent here as structured JSON. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Yes | `http://tempo:4317` | OTLP gRPC endpoint for trace export. If you don't have Tempo, set this to any valid OTLP endpoint or omit it (traces won't be exported but log correlation still works). |
| `SERVICE_NAME` | Recommended | `lyik-api` | Your service name. Used in Loki tags and Prometheus labels. |

**Docker example:**

```yaml
environment:
  - LOKI_URL=http://loki:3100
  - OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317
  - SERVICE_NAME=lyik-api
```

**Kubernetes example:**

```yaml
env:
  - name: LOKI_URL
    value: "http://loki.monitoring.svc.cluster.local:3100"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://tempo.monitoring.svc.cluster.local:4317"
  - name: SERVICE_NAME
    value: "lyik-api"
```

### 5.4 Modify tracing.py for Your Service

Open `tracing.py` and make these changes:

**Change 1: Service name in LokiHandler (line 133)**

```python
# BEFORE (hardcoded):
loki_handler = LokiHandler(
    url=f"{loki_url}/loki/api/v1/push",
    tags={"service_name": "observability-api-1"},
    version="1",
)

# AFTER (configurable):
service_name = os.getenv("SERVICE_NAME", "my-service")
loki_handler = LokiHandler(
    url=f"{loki_url}/loki/api/v1/push",
    tags={"service_name": service_name},
    version="1",
)
```

**Change 2: Path filter in ErrorExtractionFilter (line 76)**

```python
# BEFORE (hardcoded to /app/ which is the Docker container path):
app_frames = [f for f in frames if "/app/" in f.filename]

# AFTER (configurable — use your project name or a pattern):
app_root = os.getenv("APP_ROOT", "/app/")
app_frames = [f for f in frames if app_root in f.filename]
```

If you run locally (not in Docker), set `APP_ROOT` to your project directory path, e.g., `APP_ROOT="/Users/you/projects/lyik-api/"`.

**Change 3 (optional): Custom baggage fields**

If your app uses different context fields than `user_id`, `org_id`, `form_id`, `form_record_id`, update the `BAGGAGE_FIELDS` list and `TraceContextFilter`:

```python
# Line 185 — add your custom fields:
BAGGAGE_FIELDS = ["user_id", "org_id", "tenant_id", "request_id"]

# Lines 50-55 — add corresponding filter attributes:
record.user_id = get_baggage("user_id") or ""
record.org_id = get_baggage("org_id") or ""
record.tenant_id = get_baggage("tenant_id") or ""
record.request_id = get_baggage("request_id") or ""
```

**Change 4 (optional): Exclude additional paths from auto-instrumentation**

```python
# Line 113 — add paths to exclude:
def instrument_app(app):
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics,ready,info")
```

### 5.6 Integrate into Your FastAPI App

In your main FastAPI application file (e.g., `main.py`), add these lines:

```python
import json
import os
import time

from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.baggage import set_baggage
from opentelemetry.context import attach

from tracing import setup, instrument_app, log_span, enrich_span_from_context

# --- 1. Create app and instrument ---
app = FastAPI()
instrument_app(app)

# --- 2. Initialize logger (sets up Loki handler + trace context filter) ---
logger = setup()

# --- 3. Load logging config for console output ---
LOGGING_CONFIG = json.loads(open("logging_config.json").read())
```

**What each line does:**

| Line | What It Does |
|------|-------------|
| `instrument_app(app)` | Auto-instruments FastAPI — every HTTP request gets a span with `http.method`, `http.url`, `http.route`, `http.status_code` attributes |
| `setup()` | Initializes HTTPX auto-instrumentation, creates Loki handler (if `LOKI_URL` is set), adds `TraceContextFilter` to inject trace context into logs |
| `LOGGING_CONFIG` | Loads the `logging_config.json` for console output formatting (structured JSON to stderr/stdout) |

### 5.6 Add the Tracing Middleware

Add this middleware to your app. It enriches spans with request context and logs span completion:

```python
@app.middleware("http")
async def tracing_middleware(request: Request, call_next):
    span = trace.get_current_span()
    span.set_attribute("http.method", request.method)
    span.set_attribute("http.url", str(request.url))
    span.set_attribute("http.route", request.url.path)
    try:
        response = await call_next(request)
        if span.is_recording():
            enrich_span_from_context(span, request)
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(trace.StatusCode.ERROR, f"HTTP {response.status_code}")
            else:
                span.set_status(trace.StatusCode.OK)
            ctx = span.get_span_context()
            duration_ms = round((time.time() - span.start_time / 1e9) * 1000, 2) if span.start_time else 0
            log_span(span, f"{request.method} {request.url.path}",
                     duration_ms=duration_ms,
                     trace_id=format(ctx.trace_id, "032x"),
                     span_id=format(ctx.span_id, "016x"))
        return response
    except Exception as exc:
        if span.is_recording():
            enrich_span_from_context(span, request)
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR, str(exc))
            ctx = span.get_span_context()
            duration_ms = round((time.time() - span.start_time / 1e9) * 1000, 2) if span.start_time else 0
            log_span(span, f"{request.method} {request.url.path}",
                     duration_ms=duration_ms,
                     trace_id=format(ctx.trace_id, "032x"),
                     span_id=format(ctx.span_id, "016x"))
        raise exc
```

**What this middleware does:**

1. On every request, sets `http.method`, `http.url`, `http.route` as span attributes
2. After the response, calls `enrich_span_from_context()` which reads baggage fields and sets them as span attributes
3. Sets `http.status_code` and span status (`OK` for 1xx-4xx, `ERROR` for 5xx)
4. Calls `log_span()` which emits a structured `span_completed` log entry to Loki with duration, status, and trace IDs
5. On exceptions, records the exception on the span and sets ERROR status

### 5.7 Set Up Baggage in Auth/Endpoints

Baggage is how you propagate user context (`user_id`, `org_id`, etc.) through the entire request lifecycle. Every `logger.info()` call will automatically include these fields thanks to `TraceContextFilter`.

**In your auth middleware/dependency:**

```python
from opentelemetry.baggage import set_baggage
from opentelemetry.context import attach

async def require_auth(request: Request, credentials = Depends(security)):
    # ... decode your JWT / validate credentials ...

    user_id = payload.get("user_id")
    org_id = payload.get("org_id")

    # Set baggage — this propagates to all downstream spans and logs
    ctx = set_baggage("user_id", user_id)
    ctx = set_baggage("org_id", org_id, context=ctx)
    attach(ctx)

    # Also set on request.state for your business logic
    request.state.user_id = user_id
    request.state.org_id = org_id
```

**In your endpoints (optional, for additional context):**

```python
@app.post("/submit_order")
async def submit_order(request: Request, body: OrderRequest, _auth=Depends(require_auth)):
    order_id = str(uuid.uuid4())

    # Set additional baggage for this request
    ctx = set_baggage("order_id", order_id)
    attach(ctx)
    request.state.order_id = order_id

    logger.info("POST /submit_order received")  # This log will include user_id, org_id, order_id
    # ... your business logic ...
    logger.info("POST /submit_order completed")
```

**How baggage flows:**

```
Request arrives
  → require_auth sets baggage(user_id, org_id)
    → TraceContextFilter reads baggage → log line includes user_id, org_id
    → BaggageSpanProcessor copies baggage → span attributes include user_id, org_id
      → endpoint sets baggage(order_id)
        → TraceContextFilter reads baggage → log line includes user_id, org_id, order_id
        → log_span() logs span completion → structured log includes all attributes
```

### 5.8 Add the Metrics Endpoint

Add a `/metrics` endpoint that Prometheus can scrape:

```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import PlainTextResponse

@app.get("/metrics")
async def metrics():
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

**Prometheus scrape config** (add to your `prometheus.yml`):

```yaml
scrape_configs:
  - job_name: "lyik-api"
    scrape_interval: 15s
    static_configs:
      - targets: ["lyik-api:8000"]
```

**Available metrics:**

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `app_requests_total` | Counter | `span_name`, `status` | Total request count. Only incremented if you use the `@traced` decorator. |
| `app_request_duration_seconds` | Histogram | `span_name` | Request duration. Only observed if you use the `@traced` decorator. |

**Note:** The middleware logs span metadata to Loki but does NOT update Prometheus metrics. To get Prometheus metrics, use the `@traced` decorator on your endpoints (see below).

**Using the `@traced` decorator for metrics:**

```python
from tracing import traced

@traced("POST /submit_order")
async def submit_order(request: Request, body: OrderRequest, _auth=Depends(require_auth)):
    # ... your logic ...
    # On success: REQUEST_COUNT.labels(span_name="POST /submit_order", status="ok").inc()
    # On exception: REQUEST_COUNT.labels(span_name="POST /submit_order", status="error").inc()
    # Duration: REQUEST_DURATION.labels(span_name="POST /submit_order").observe(duration)
    pass
```

The `@traced` decorator:
- Creates a child span under the current request span
- Sets span status (OK on success, ERROR on exception)
- Logs span completion to Loki
- Increments `REQUEST_COUNT` counter
- Records duration in `REQUEST_DURATION` histogram

### 5.9 Configure Structured Logging

The `logging_config.json` file configures Python's `dictConfig`. It sets up:

- **JSON formatter** — every log line is a single JSON object (parseable by Loki)
- **ErrorExtractionFilter** — on ERROR+ logs, extracts exception details into a structured `error` field
- **Console output** — stderr for app logs, stdout for uvicorn access logs

**What a log line looks like in Loki:**

```json
{
  "timestamp": "2025-01-15T10:30:00.000Z",
  "level": "INFO",
  "name": "app",
  "message": "POST /submit_order completed",
  "trace_id": "abc123def456...",
  "span_id": "789ghi012...",
  "user_id": "alice",
  "org_id": "acme",
  "order_id": "ord-123"
}
```

**What an error log looks like:**

```json
{
  "timestamp": "2025-01-15T10:30:00.000Z",
  "level": "ERROR",
  "name": "app",
  "message": "Database connection failed",
  "trace_id": "abc123def456...",
  "span_id": "789ghi012...",
  "user_id": "alice",
  "org_id": "acme",
  "error": {
    "type": "ConnectionFailure",
    "message": "Connection refused (url=postgres://db:5432/mydb)",
    "location": "/app/services/database.py:42 in connect",
    "stack_trace": ["File \"/app/services/database.py\", line 42, in connect\n    raise ConnectionFailure(msg)"],
    "stack_trace_full": ["Traceback (most recent call last)..."]
  }
}
```

**If you need to adjust logger names** (e.g., your app uses a different logger name than `"app"`):

Edit `logging_config.json`:

```json
{
  "loggers": {
    "lyik": {"handlers": ["default"], "level": "INFO", "propagate": false},
    "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": false},
    "uvicorn.error": {"level": "INFO"},
    "uvicorn.access": {"handlers": ["access"], "level": "WARNING", "propagate": false}
  }
}
```

Then in your code, use `logging.getLogger("lyik")` or set `setup(name="lyik")`.

---

## 6. Grafana Dashboard Setup

### 6.1 Import Dashboards

**Option A: UI Import (recommended for first-time setup)**

1. Open Grafana → Dashboards → Import
2. Upload `grafana/provisioning/dashboards/disabled/observability-api.json` (the full dashboard with Log Volume, Logs, Trace Search, Request Rate, and Duration panels)
3. Select your Loki and Prometheus datasources when prompted
4. Click Import

**Option B: File-based provisioning**

If you use Grafana's file-based provisioning:

1. Copy `internal_dashboard.json` and `internal_dashboard_wrapped.json` to your Grafana provisioning directory
2. Ensure `dashboard.yml` points to the directory containing the JSON files
3. Restart Grafana

### 6.2 Update Datasource UIDs

The dashboard JSONs contain hardcoded datasource UIDs. You need to update these to match your Grafana instance.

**Finding your datasource UIDs:**

1. Grafana → Settings → Data Sources → click on your Loki/Prometheus/Tempo datasources
2. The UID is in the URL: `grafana/org/1/datasources/edit/P8E80F9AEF21F6940` → UID is `P8E80F9AEF21F6940`

**In the dashboard JSON, find and replace:**

| Old UID | DataSource | Replace With |
|---------|-----------|-------------|
| `P8E80F9AEF21F6940` | Loki | Your Loki datasource UID |
| `tempo` | Tempo | Your Tempo datasource UID (if using Tempo) |
| `prometheus` | Prometheus | Your Prometheus datasource UID |

**Using Grafana UI (easier):**

After importing the dashboard, each panel will show a datasource warning. Click on the panel → Edit → select your correct datasource → Save.

### 6.3 Update Service Name in Queries

The dashboard Loki queries are hardcoded to `observability-api-1`. Update them to your service name.

**In the dashboard JSON, find and replace:**

```
service_name="observability-api-1"
```

With:

```
service_name="lyik-api"
```

This appears in:
- Log Volume panel query
- Logs panel query
- Level variable query

**Using Grafana UI:**

After importing, click on each panel → Edit → update the Loki query → Save.

### 6.4 File-Based Provisioning (Optional)

For automated dashboard deployment, use Grafana's file-based provisioning.

**Directory structure:**

```
grafana/
├── provisioning/
│   ├── datasources/
│   │   └── datasource.yml
│   └── dashboards/
│       ├── dashboard.yml
│       └── json/
│           ├── internal_dashboard.json
│           ├── internal_dashboard_wrapped.json
│           └── observability-api.json
```

**datasource.yml:**

```yaml
apiVersion: 1
datasources:
  - name: Loki
    type: loki
    access: proxy
    url: http://your-loki-host:3100
    isDefault: true
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://your-prometheus-host:9090
    uid: prometheus
```

**dashboard.yml:**

```yaml
apiVersion: 1
providers:
  - name: 'default'
    orgId: 1
    folder: ''
    type: file
    disableDeletion: false
    editable: true
    options:
      path: /etc/grafana/provisioning/dashboards/json
      foldersFromFilesStructure: false
```

**Important:** The dashboard JSON files must be in **Grafana v2 K8s resource format** for provisioning to work with Grafana 12.4+/13.x:

```json
{
  "apiVersion": "dashboard.grafana.app/v2",
  "kind": "Dashboard",
  "metadata": {
    "name": "your-dashboard-name"
  },
  "spec": {
    ... your dashboard content ...
  }
}
```

---

## 7. Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LOKI_URL` | Yes | — | Loki push endpoint (e.g., `http://loki:3100`). If not set, logs go to console only. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Recommended | — | OTLP gRPC endpoint for trace export (e.g., `http://tempo:4317`). If not set, traces are created but not exported. |
| `SERVICE_NAME` | Recommended | `my-service` | Service name used in Loki tags and Prometheus labels. |
| `APP_ROOT` | Optional | `/app/` | Root path for error stack trace filtering in `ErrorExtractionFilter`. Set to your project root if not running in Docker. |

### tracing.py API Reference

| Function/Class | Signature | Description |
|---------------|-----------|-------------|
| `instrument_app(app)` | `instrument_app(app: FastAPI)` | Auto-instruments FastAPI. Call once after creating the app. |
| `setup(name, level, propagate)` | `setup(name="app", level=INFO, propagate=False) -> Logger` | Initializes HTTPX instrumentor, Loki handler, and TraceContextFilter. Returns configured logger. |
| `log_span(span, span_name, ...)` | `log_span(span, span_name, logger=None, duration_ms=None, trace_id=None, span_id=None, **extra_attrs)` | Logs span completion metadata as a structured `span_completed` log entry. |
| `enrich_span_from_context(span, request)` | `enrich_span_from_context(span, request) -> dict` | Reads baggage from context, sets span attributes, re-attaches baggage. Returns resolved fields. |
| `@traced(span_name, skip_paths)` | `@traced("endpoint name")` or `@traced(lambda req: f"{req.method} {req.url.path}")` | Decorator: creates a span, sets status, logs completion, updates Prometheus metrics. |
| `TraceContextFilter` | `logging.Filter` | Auto-injects `trace_id`, `span_id`, and baggage fields into every log record. |
| `ErrorExtractionFilter` | `logging.Filter` | On ERROR+ logs, extracts exception details into structured `error` JSON field. |
| `REQUEST_COUNT` | `prometheus_client.Counter` | `app_requests_total` counter with labels `span_name`, `status`. |
| `REQUEST_DURATION` | `prometheus_client.Histogram` | `app_request_duration_seconds` histogram with label `span_name`. |

### Log Fields Available in Loki

Every log line automatically includes these fields (via `TraceContextFilter`):

| Field | Type | Description |
|-------|------|-------------|
| `trace_id` | string (32 hex) | OpenTelemetry trace ID — links log to its trace |
| `span_id` | string (16 hex) | OpenTelemetry span ID — links log to its specific span |
| `user_id` | string | From baggage — the authenticated user |
| `org_id` | string | From baggage — the user's organization |
| `form_id` | string | From baggage (if set) — the form being processed |
| `form_record_id` | string | From baggage (if set) — the specific form record |

**Loki queries you can now run:**

```logql
# All logs for a service
{service_name="lyik-api"} | json

# Logs for a specific user
{service_name="lyik-api"} | json | user_id="alice"

# Logs for a specific trace (trace a request end-to-end)
{service_name="lyik-api"} | json | trace_id="abc123def456..."

# Error logs only
{service_name="lyik-api"} | json | level="ERROR"

# Logs with specific error type
{service_name="lyik-api"} | json | level="ERROR" | error.type="ConnectionFailure"

# Logs for a specific endpoint
{service_name="lyik-api"} | json | span_name=~"POST /submit_order.*"

# Logs for a specific org
{service_name="lyik-api"} | json | org_id="acme"
```

---

## 8. How It All Works Together

### Request Lifecycle with Observability

```
1. HTTP Request arrives at FastAPI
   │
   ├─ FastAPIInstrumentor creates a span (auto)
   │   └─ Span gets: http.method, http.url, http.route (from middleware)
   │
2. Auth dependency runs
   │
   ├─ require_auth sets baggage(user_id, org_id)
   │   └─ BaggageSpanProcessor copies to span attributes
   │
3. Endpoint runs
   │
   ├─ Your logger.info("processing request")
   │   └─ TraceContextFilter adds: trace_id, span_id, user_id, org_id
   │   └─ LokiHandler pushes structured JSON to Loki
   │
   ├─ HTTPX call to external service (if any)
   │   └─ HTTPXClientInstrumentor creates child span
   │       └─ Trace context propagated via headers
   │
   ├─ Your logger.info("request completed")
   │   └─ Same trace_id/span_id/user_id/org_id in log
   │
4. Response sent
   │
   ├─ Middleware sets http.status_code on span
   ├─ Middleware calls log_span() → "span_completed" log to Loki
   ├─ Middleware calls enrich_span_from_context() → baggage re-attached
   │
5. Prometheus scrapes /metrics
   │
   └─ Reads app_requests_total, app_request_duration_seconds
```

### What Each Component Does

| Component | What It Does | When |
|-----------|-------------|------|
| `FastAPIInstrumentor` | Creates a span for every HTTP request | Request arrives |
| `HTTPXClientInstrumentor` | Creates child spans for outbound HTTP calls | HTTPX request made |
| `BaggageSpanProcessor` | Copies baggage fields → span attributes | Span created |
| `TraceContextFilter` | Injects trace_id/span_id/baggage into log records | Every logger call |
| `ErrorExtractionFilter` | Extracts exception details into structured error field | ERROR+ log |
| `LokiHandler` | Pushes structured JSON log to Loki | Every logger call |
| `tracing_middleware` | Sets HTTP attributes, logs span completion, enriches spans | Request/response |
| `@traced` decorator | Creates child span, logs completion, updates Prometheus metrics | Decorated function |
| `log_span()` | Emits structured `span_completed` log entry | Called by middleware/decorator |
| `enrich_span_from_context()` | Reads baggage → span attributes, re-attaches baggage | Called by middleware |

---

## 9. Troubleshooting

### Logs not appearing in Loki

1. **Check `LOKI_URL` is set:**
   ```bash
   echo $LOKI_URL
   # Should output something like: http://loki:3100
   ```

2. **Check Loki is reachable from your app:**
   ```bash
   curl -v http://your-loki-host:3100/ready
   # Should return: ready
   ```

3. **Check logs are structured JSON:**
   ```bash
   # Run your app locally and check console output
   # Logs should be single-line JSON objects, not plain text
   ```

4. **Check Loki labels:**
   - In Grafana → Explore → Loki → check if `service_name` label exists
   - The `service_name` must match what's in your Loki query

### trace_id showing as all zeros

```json
{
  "trace_id": "00000000000000000000000000000000",
  "span_id": "0000000000000000"
}
```

This means the span was created outside of an OpenTelemetry context. Causes:
- `FastAPIInstrumentor` not called before request handling
- `setup()` not called at startup
- Using `logging.getLogger()` directly instead of `setup()` return value

**Fix:** Ensure `instrument_app(app)` and `setup()` are called before any request handling.

### Prometheus metrics are empty

1. **Check `/metrics` endpoint:**
   ```bash
   curl http://localhost:8000/metrics
   # Should output Prometheus text format with app_requests_total, etc.
   ```

2. **Check Prometheus scrape config:**
   ```yaml
   scrape_configs:
     - job_name: "lyik-api"
       static_configs:
         - targets: ["lyik-api:8000"]
   ```

3. **Remember:** The middleware does NOT update Prometheus metrics. You need the `@traced` decorator on endpoints for metrics to be recorded.

### Error stack traces missing in Loki

1. **Check `APP_ROOT` is set correctly:**
   ```bash
   echo $APP_ROOT
   # Should match your project's root path
   ```

2. **Check that `exc_info=True` is set on your log calls:**
   `ErrorExtractionFilter` only activates when `record.exc_info` is truthy. This does NOT happen automatically for `logger.error()` or `logger.warning()`. You must explicitly pass `exc_info=True` or use `logger.exception()`:

   ```python
   # WRONG — filter won't fire, record.error will be null:
   logger.error("request failed")

   # RIGHT — one of these:
   logger.error("request failed", exc_info=True)
   logger.exception("request failed")  # same as exc_info=True
   ```

   The only exception is `logger.exception()` which sets `exc_info=True` by default:

   ```python
   try:
       raise ValueError("bad input")
   except ValueError:
       logger.exception("something broke")  # exc_info=True automatically
   ```

   Inside an `except` block, if you use `logger.error()` without `exc_info=True`, the exception info is lost and the filter produces `"error": null`.

### Baggage fields not appearing in logs

1. **Check that `set_baggage`/`attach` is called before logging:**
   ```python
   # WRONG — logging before baggage is set:
   logger.info("processing")
   ctx = set_baggage("user_id", user_id)
   attach(ctx)

   # RIGHT — baggage set before logging:
   ctx = set_baggage("user_id", user_id)
   attach(ctx)
   logger.info("processing")  # Now includes user_id
   ```

2. **Check `TraceContextFilter` is attached to your logger:**
   - `setup()` adds it automatically
   - If you create loggers manually, add it: `logger.addFilter(TraceContextFilter())`

---

## 10. FAQ

### Q: Do I need Tempo for this to work?

**No.** Tempo is optional. Without Tempo:
- Structured logs with `trace_id`/`span_id` still work in Loki
- You can still filter logs by `user_id`, `org_id`, etc.
- Spans are created but not exported anywhere visible
- You lose: trace timeline visualization, service graphs, trace-to-log drill-down from Grafana's TraceQL

With Tempo, you get the full three-pillar experience: logs → traces → metrics all correlated.

### Q: Can I use this with an existing plain-text log pipeline?

**Yes.** The `LokiHandler` sends structured JSON to Loki. Your existing plain-text logs (via Promtail, Fluentd, etc.) continue working alongside. The structured JSON logs are additional — they don't replace your existing logs.

### Q: What if I already have a logging setup?

The `setup()` function creates a new logger or configures an existing one. If you already have logging configured:
- Call `setup(name="your-existing-logger-name")` to reuse your existing logger
- The `TraceContextFilter` and `LokiHandler` will be added to it
- Your existing handlers/formaters remain untouched

### Q: Can I use this with Django instead of FastAPI?

Not directly. `FastAPIInstrumentor` is FastAPI-specific. For Django:
- Use `opentelemetry-instrumentation-django` instead
- Replace `tracing_middleware` with Django middleware
- The rest of `tracing.py` (filters, Loki handler, `@traced` decorator) works as-is

### Q: Can I use this with SQLAlchemy or other ORMs?

**Yes.** Add `opentelemetry-instrumentation-sqlalchemy` to your dependencies and instrument your engine:

```python
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

SQLAlchemyInstrumentor().instrument(engine=your_engine)
```

This creates child spans for every SQL query, visible in your trace timeline (if using Tempo).

### Q: How do I add custom span attributes?

In your endpoint or anywhere you have a current span:

```python
from opentelemetry import trace

span = trace.get_current_span()
span.set_attribute("order.total", 99.99)
span.set_attribute("order.item_count", 5)
```

These will appear in the trace (if using Tempo) and in `span_completed` log entries.

### Q: How do I create custom spans for specific operations?

Use the `@traced` decorator or create spans manually:

```python
from tracing import traced
from opentelemetry import trace

# Option 1: Decorator
@traced("process_payment")
async def process_payment(order_id, amount):
    # ... payment logic ...
    pass

# Option 2: Manual span
tracer = trace.get_tracer(__name__)

async def process_payment(order_id, amount):
    with tracer.start_as_current_span("process_payment") as span:
        span.set_attribute("order.id", order_id)
        span.set_attribute("payment.amount", amount)
        # ... payment logic ...
```

### Q: What happens if Loki is down?

Logs are still written to console (stderr/stdout) via the `logging.StreamHandler`. The `LokiHandler` will fail silently — it doesn't block your application. Logs are lost for Loki but your app continues running.

### Q: Can I filter out sensitive fields from logs?

Use Python's `logging.Filter` or `python-json-logger`'s `redirect` processor:

```python
class SanitizeFilter(logging.Filter):
    def filter(self, record):
        if hasattr(record, 'password'):
            record.password = '***'
        return True

logger.addFilter(SanitizeFilter())
```

Or use `python-json-logger`'s `ProcessorFormatter` with `wipe` or `rename` processors.

---

## Quick Reference: Minimal Integration Checklist

For the fastest possible integration into an existing FastAPI app:

- [ ] Copy `tracing.py` to your project root
- [ ] Copy `logging_config.json` to your project root
- [ ] Add dependencies to `requirements.txt`
- [ ] Run `pip install -r requirements.txt`
- [ ] Set `LOKI_URL` and `SERVICE_NAME` environment variables
- [ ] In `tracing.py`: update `service_name` in `LokiHandler` tags (line 133)
- [ ] In your `main.py`:
  ```python
  from tracing import setup, instrument_app
  app = FastAPI()
  instrument_app(app)
  logger = setup()
  ```
- [ ] Add the `tracing_middleware` to your app
- [ ] Add `set_baggage`/`attach` calls in your auth dependency
- [ ] Add `/metrics` endpoint for Prometheus
- [ ] Import dashboard JSON into Grafana, update datasource UIDs and service name
- [ ] Test: hit an endpoint, check Loki for structured logs with `trace_id`
