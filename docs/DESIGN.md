# Observability Pipeline — Design Document

## Overview

This document describes the current observability pipeline implemented in the FastAPI prototype application. The pipeline covers structured JSON logging, OpenTelemetry tracing with OTLP export, Prometheus metrics, and log aggregation via direct Loki push → Grafana.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       FastAPI Application                        │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │              Tracing Middleware (@traced)                │   │
│   │   Creates root span per request, covers full lifecycle  │   │
│   └─────────────────────────┬───────────────────────────────┘   │
│                             │                                     │
│   ┌────────────┐    ┌──────┴─────┐    ┌────────────┐           │
│   │ form_A      │───▶│ function_b  │───▶│ function_c  │          │
│   │  [span]     │    │  [span]     │    │  [span]     │          │
│   └────────────┘    └─────┬──────┘    └────────────┘           │
│                           │                                      │
│                    httpx POST to                                 │
│                    localhost:8000/process                        │
│                                                                  │
│   ┌────────────────────────────────────────────────────────┐    │
│   │                   Logging Pipeline                      │    │
│   │                                                         │    │
│   │  LogRecord ──▶ Filters ──▶ JsonFormatter ──▶ StreamHandler│  │
│   │                  │                   │                   │    │
│   │          ┌───────┼───────────┐       │                   │    │
│   │          ▼       ▼           ▼       ▼                   │    │
│   │     TraceCtx  ErrorExtract  UserId  LokiHandler         │    │
│   │     Filter    Filter        Filter  (direct push)       │    │
│   │    (OTel span) (exc_info)   (ContextVar)                │    │
│   └────────────────────────────────┬───────────────────────┘    │
│                                    │                             │
│                          ┌─────────┴──────────┐                 │
│                          ▼                    ▼                 │
│                   stdout/stderr         Loki HTTP API           │
│                   (console)            (direct push)            │
│                                                                  │
│   ┌────────────────────────────────────────────────────────┐    │
│   │                   Traces Pipeline                       │    │
│   │                                                         │    │
│   │  OTel SDK ──▶ BatchSpanProcessor ──▶ OTLPSpanExporter  │    │
│   └────────────────────────────┬───────────────────────────┘    │
│                                │ gRPC                            │
│                                ▼                                 │
│                         Tempo (OTLP)                             │
│                                                                  │
│   ┌────────────────────────────────────────────────────────┐    │
│   │                  Metrics Pipeline                       │    │
│   │                                                         │    │
│   │  prometheus_client ──▶ /metrics endpoint                │    │
│   │  + @traced decorator records counters + histograms      │    │
│   └────────────────────────────┬───────────────────────────┘    │
│                                │ scrape                          │
│                                ▼                                 │
│                        Prometheus                                │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼              ▼              ▼
        ┌────────────┐ ┌────────────┐ ┌────────────┐
        │    Loki    │ │   Tempo    │ │ Prometheus │
        │  (logs)    │ │ (traces)   │ │ (metrics)  │
        └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                      ┌────────────┐
                      │  Grafana   │  auto-provisioned dashboards
                      │  (port 3000)│
                      └────────────┘
```

---

## Components

### 1. FastAPI Application (`main.py`)

**Framework:** FastAPI with uvicorn ASGI server.

**Key modules:**
- Custom `tracing_middleware` — creates root span per request via `@traced` decorator
- `@traced` decorator — manual span creation with Prometheus metrics recording
- `TraceContextFilter` — auto-injects `trace_id`, `span_id`, `user_id`, `org_id`, `form_id`, `form_record_id` from OTel baggage
- `ErrorExtractionFilter` — converts `exc_info` into structured error object with split stack traces

**Endpoints:**

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/signin` | POST | JWT authentication against MongoDB, sets `user_id`/`org_id` in baggage |
| `/start` | POST | Orchestrates form_A → function_b chain |
| `/process` | POST | Invokes function_c (always errors) |
| `/form_A` | POST | Form submission — inserts record to MongoDB |
| `/form_B` | POST | Form submission — 50% random rejection before DB insert |
| `/metrics` | GET | Prometheus metrics endpoint |

**Internal functions:**

| Function | Description |
|----------|-------------|
| `function_a` | Simulates work (1s sleep) |
| `function_b` | Simulates work, calls `/process` via HTTPX |
| `function_c` | Simulates work, raises `ValueError` (test error) |
| `start_process` | Orchestrator, calls function_a then function_b |

**Trace propagation:** OTel context propagated via custom middleware. `enrich_span_from_context()` resolves baggage with `request.state` fallback.

---

### 2. Structured JSON Logging

**Formatter:** `pythonjsonlogger.jsonlogger.JsonFormatter`

**Console logging** is configured via `logging_config.json` (loaded by uvicorn's `--log-config`). The `setup()` function in `tracing.py` only handles:
- Loki handler (when `LOKI_URL` is set)
- Logger-level filters (`TraceContextFilter`)

#### Logger Configuration

From `logging_config.json`:

| Logger | Handler | Purpose |
|--------|---------|---------|
| `app` | `default` (stderr) + Loki | Application logs |
| `uvicorn` | `default` (stderr) | Server logs |
| `uvicorn.access` | `access` (stdout) | Request logs |

Root logger: `handlers: []`, `level: WARNING`, `propagate: false` — prevents duplicate console output.

---

### 3. Log Filters

Three custom filters enrich log records:

| Filter | Source | Adds to Record | Purpose |
|--------|--------|----------------|---------|
| `TraceContextFilter` | `tracing.py` | `trace_id`, `span_id`, `user_id`, `org_id`, `form_id`, `form_record_id` | Reads current OTel span context + baggage |
| `ErrorExtractionFilter` | `tracing.py` | `error` (dict) | Converts `exc_info` into structured error object |
| `UserIdFilter` | (removed) | — | Superseded by `TraceContextFilter` baggage injection |

**Filter wiring:**

| Logger | TraceContextFilter | ErrorExtractionFilter |
|--------|:------------------:|:---------------------:|
| `app` (dict) | Yes (handler-level) | Yes (handler-level) |
| `uvicorn` (dict) | No | Yes (handler-level) |
| `uvicorn.access` (dict) | No | Yes (handler-level) |

---

### 4. Error Extraction

`ErrorExtractionFilter` intercepts ERROR-level log records containing `exc_info` and produces:

```json
{
  "error": {
    "type": "ValueError",
    "message": "test error to verify error extraction",
    "location": "/app/main.py:191 in function_c",
    "stack_trace": ["  File \"/app/main.py\", line 191, in function_c", "    raise ValueError(...)"],
    "stack_trace_full": ["  File \"/app/main.py\", line 191, in function_c", "..."]
  }
}
```

**Key behaviors:**
- `stack_trace` — app frames only (filtered to `/app/` paths), no library/venv noise
- `stack_trace_full` — complete traceback (all frames)
- Caret lines (`^^^^`) removed from both stack traces (Python 3.11+)
- `location` — last app frame closest to exception origin
- `message` — falls back to `__cause__` message if primary is empty; includes URL for httpx exceptions
- Clears `record.exc_info` and `record.exc_text` to prevent Python's raw traceback output

---

### 5. JSON Log Output Format

Every application log produces a single-line JSON object:

```json
{
  "timestamp": "2026-08-31T12:00:00.000Z",
  "level": "INFO",
  "name": "app",
  "message": "form_A completed",
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "span_id": "00f067aa0ba902b7",
  "user_id": "user1",
  "org_id": "org1",
  "form_id": "form_A",
  "form_record_id": "abc123",
  "error": null
}
```

**Fields:**

| Field | Source | Always Present |
|-------|--------|:--------------:|
| `timestamp` | `asctime` renamed | Yes |
| `level` | `levelname` renamed | Yes |
| `name` | Logger name | Yes |
| `message` | Log message | Yes |
| `trace_id` | `TraceContextFilter` from OTel span | Yes (all zeros if no span) |
| `span_id` | `TraceContextFilter` from OTel span | Yes (all zeros if no span) |
| `user_id` | `TraceContextFilter` from baggage | Yes (empty string if unset) |
| `org_id` | `TraceContextFilter` from baggage | Yes (empty string if unset) |
| `form_id` | `TraceContextFilter` from baggage | Yes (empty string if unset) |
| `form_record_id` | `TraceContextFilter` from baggage | Yes (empty string if unset) |
| `error` | `ErrorExtractionFilter` | Only on ERROR with exception |

---

### 6. OpenTelemetry Tracing

**Provider:** `opentelemetry.sdk.trace.TracerProvider` with `Resource` (`service.name=observability-api-1`).

**Exporter:** `OTLPSpanExporter` → Tempo at `http://tempo:4317` (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set).

**Processor:** `BatchSpanProcessor` — batches spans before export.

**Instrumentation:**
- Custom `tracing_middleware` — creates root span per request via `@traced("HTTP")`
- `HTTPXClientInstrumentor().instrument()` — creates spans for outgoing HTTPX requests

**Manual spans:**
- `@traced` decorator records `app_requests_total` counter and `app_request_duration_seconds` histogram

**Span hierarchy:**
```
HTTP (middleware root span)
  ├── form_A / form_B
  │     └── function_b
  │           └── HTTP POST /process (auto-instrumented)
  │                 └── function_c
  └── /signin
```

**Trace context propagation:** OTel context via W3C Trace Context headers. Baggage carries `user_id`, `org_id`, `form_id`, `form_record_id`. Middleware uses `enrich_span_from_context()` to resolve baggage with `request.state` fallback.

---

### 7. Log Aggregation Pipeline

#### Direct Loki Push (App → Loki)

- **Mechanism:** `python-logging-loki` library, `LokiHandler` added to app logger
- **Destination:** Loki at `http://loki:3100/loki/api/v1/push`
- **Triggered by:** `LOKI_URL` environment variable set
- **Labels:** `service_name` (from OTel Resource)

#### Loki

- **Mode:** Single-instance, auth disabled
- **Storage:** Filesystem (chunks + rules)
- **Index:** TSDB with schema v13, 24h period
- **Ring:** In-memory (no clustering)

---

### 8. Trace Backend (Tempo)

- **Receiver:** OTLP gRPC on port 4317, HTTP on port 4318
- **Storage:** Local filesystem
- **Datasource:** Auto-provisioned in Grafana

---

### 9. Metrics Backend (Prometheus)

- **Scrape:** Prometheus scrapes `/metrics` endpoint on the API service
- **Metrics recorded:**
  - `app_requests_total` (counter, labels: `span_name`, `status`)
  - `app_request_duration_seconds` (histogram, label: `span_name`)
- **Datasource:** Auto-provisioned in Grafana

---

### 10. Grafana Dashboard

Auto-provisioned via `grafana/provisioning/dashboards/json/observability-api.json`:

| Panel | Type | Description |
|-------|------|-------------|
| Log Volume | Timeseries bar chart | Log count over time |
| Logs | Log stream | Live log stream with level filter, `wrapLogMessage: true` |
| Traces | TraceQL search | Trace search panel |
| Request Rate | Time series | Requests per second |
| Request Duration | Time series | p50/p95 latency |

**Datasources auto-provisioned:** Loki, Tempo, Prometheus

---

## Docker Infrastructure

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `api` | Custom (Python 3.12-slim) | 8000 | FastAPI application |
| `mongodb` | `mongo:7` | 27017 | User data store |
| `loki` | `grafana/loki:latest` | 3100 | Log aggregation |
| `tempo` | `grafana/tempo:latest` | 3200, 4317, 4318 | Trace backend |
| `prometheus` | `prom/prometheus:latest` | 9090 | Metrics backend |
| `grafana` | `grafana/grafana:latest` | 3000 | Visualization |

**Network:** All services on `observability-network`.

---

## Data Flow Summary

```
Request arrives
  │
  ▼
tracing_middleware creates root span (@traced("HTTP"))
  │
  ▼
Auth dependency sets user_id in ContextVar + request.state
  │
  ▼
Endpoint handler creates manual span, sets baggage
  │
  ▼
Internal functions create child spans
  │
  ▼
enrich_span_from_context() resolves baggage → span attributes
  │
  ▼
TraceContextFilter reads OTel span + baggage → injects into LogRecord
  │
  ▼
JsonFormatter serializes to JSON on stdout/stderr
  │
  ▼
LokiHandler pushes logs directly to Loki (when LOKI_URL set)
  │
  ▼
BatchSpanProcessor exports spans to Tempo via OTLP gRPC
  │
  ▼
Prometheus scrapes /metrics endpoint
  │
  ▼
Grafana queries Loki + Tempo + Prometheus for visualization
```

---

## Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| `LOKI_URL` | `http://loki:3100` | Loki endpoint for log export |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://tempo:4317` | Tempo endpoint for trace export |
| `MONGO_URL` | `mongodb://mongodb:27017` | MongoDB connection |
| `JWT_SECRET_KEY` | `dev-secret-change-in-prod` | JWT signing key |

---

## Known Gaps

| Gap | Impact | Location |
|-----|--------|----------|
| No DB operation spans | DB calls not traced | `db.py` |
| No external API call spans | External calls not individually traced | `main.py` |
| `ErrorExtractionFilter` can crash silently | Filter removes itself from logger for rest of session | `tracing.py` |
| `start_process()` has no span | Gap in trace hierarchy | `main.py` |
| LogQL nested field queries require `\| json` parser | Cannot use label matchers on `error.type` directly | Grafana queries |

---

## Key Decisions

1. **Custom middleware over FastAPI instrumentor** — production code uses custom approach
2. **Direct Loki push over Alloy** — simpler architecture, one less service
3. **Traces first, metrics later** — incremental approach to full observability
4. **dictConfig for console logging** — single source of truth, `setup()` only handles Loki + filters
5. **Baggage-based log context** — `form_record_id`, `form_id` auto-injected via filter, no manual `extra={}`
6. **request.state as baggage fallback** — OTel context doesn't propagate back from endpoint handlers to middleware in async FastAPI; `request.state` is immune to this
7. **Split stack_trace into app-only + full** — `stack_trace` = filtered app frames for quick scanning; `stack_trace_full` = complete trace for deep debugging
8. **Nested error object over flat fields** — keeps logs smaller; query via LogQL `| json | label_format` at query time
9. **Single ErrorExtractionFilter on handlers** — avoids duplicate filter overwriting `record.error`
