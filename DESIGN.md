# Observability Pipeline — Design Document

## Overview

This document describes the current observability pipeline implemented in the FastAPI prototype application. The pipeline covers structured JSON logging, OpenTelemetry tracing, and log aggregation via Grafana Alloy → Loki → Grafana.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       FastAPI Application                        │
│                                                                  │
│   ┌────────────┐    ┌────────────┐    ┌────────────┐           │
│   │ function_a  │───▶│ function_b  │───▶│ function_c  │          │
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
│   │                  │                                     │    │
│   │          ┌───────┼───────────┐                         │    │
│   │          ▼       ▼           ▼                         │    │
│   │     UserIdFilter  TraceCtx  ErrorExtractFilter         │    │
│   │     (ContextVar)  Filter    (exc_info → error dict)    │    │
│   │                  (OTel span)                            │    │
│   └────────────────────────────────┬───────────────────────┘    │
│                                    │                             │
│                                    ▼                             │
│                           JSON on stdout/stderr                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │
                               │  Docker container logs
                               ▼
                      ┌────────────────┐
                      │  Grafana Alloy │  scrape via Docker socket
                      └───────┬────────┘
                              │  HTTP push
                              ▼
                      ┌────────────────┐
                      │     Loki       │  TSDB + filesystem
                      └───────┬────────┘
                              │  query
                              ▼
                      ┌────────────────┐
                      │    Grafana     │  Loki datasource (auto-provisioned)
                      └────────────────┘
```

---

## Components

### 1. FastAPI Application (`main.py`)

**Framework:** FastAPI with uvicorn ASGI server.

**Key modules:**
- `FastAPIInstrumentor` — auto-instruments incoming HTTP requests with spans
- `HTTPXClientInstrumentor` — auto-instruments outgoing HTTPX requests with spans
- `TracerProvider` — OpenTelemetry SDK tracer provider (default, in-process)
- `tracer` — module-level tracer for manual span creation

**Endpoints:**

| Endpoint | Method | Span Name | Description |
|----------|--------|-----------|-------------|
| `/signin` | POST | (none) | JWT authentication against MongoDB |
| `/start` | POST | `start_endpoint` | Orchestrates function_a → function_b chain |
| `/process` | POST | `process_endpoint` | Invokes function_c (always errors) |

**Internal functions:**

| Function | Span Name | Description |
|----------|-----------|-------------|
| `function_a` | `function_a` | Simulates work (1s sleep) |
| `function_b` | `function_b` | Simulates work, calls `/process` via HTTPX |
| `function_c` | `function_c` | Simulates work, raises `ValueError` (test error) |
| `start_process` | (none) | Orchestrator, calls function_a then function_b |

**Trace propagation:** OpenTelemetry context is propagated automatically via `FastAPIInstrumentor` (incoming) and `HTTPXClientInstrumentor` (outgoing). Manual spans use `tracer.start_as_current_span()`.

---

### 2. Structured JSON Logging

**Formatter:** `pythonjsonlogger.jsonlogger.JsonFormatter`

**Two logging pipelines exist:**

#### A. App Logger (code-configured)

Defined in `main.py` (lines 118–131):

```
handler = logging.StreamHandler()
handler.setFormatter(formatter)
handler.addFilter(UserIdFilter())
handler.addFilter(ErrorExtractionFilter())
```

Logger: `"app"`, level INFO, `propagate=False`.

#### B. Uvicorn Loggers (dict-configured)

Defined in `logging_config.json`, loaded at line 133, passed to uvicorn via `log_config`.

Loggers configured:
- `uvicorn` → `default` handler (stderr)
- `uvicorn.error` → inherits from `uvicorn`
- `uvicorn.access` → `access` handler (stdout)

---

### 3. Log Filters

Three custom filters enrich log records:

| Filter | Source | Adds to Record | Purpose |
|--------|--------|----------------|---------|
| `UserIdFilter` | `main.py:80` | `user_id` | Reads `ContextVar` set by auth dependency |
| `TraceContextFilter` | `main.py:109` | `trace_id`, `span_id` | Reads current OTel span context |
| `ErrorExtractionFilter` | `main.py:86` | `error` (dict) | Converts `exc_info` into structured error object |

**Filter wiring:**

| Logger | UserIdFilter | TraceContextFilter | ErrorExtractionFilter |
|--------|:------------:|:------------------:|:---------------------:|
| `app` (code) | Yes | No* | Yes |
| `uvicorn` (dict) | No | No | Yes |
| `uvicorn.access` (dict) | No | No | Yes |

> *The `app` logger compensates for the missing `TraceContextFilter` via the `log()` helper, which manually injects `trace_id`/`span_id` as `extra` kwargs.

---

### 4. Error Extraction

`ErrorExtractionFilter` intercepts ERROR-level log records containing `exc_info` and produces:

```json
{
  "error": {
    "type": "ValueError",
    "message": "test error to verify error extraction",
    "location": "/app/main.py:191 in function_c",
    "stack_trace": "Traceback (most recent call last):\n  ..."
  }
}
```

The filter then clears `record.exc_info` and `record.exc_text` to prevent Python's default raw traceback output.

---

### 5. JSON Log Output Format

Every application log produces a single-line JSON object:

```json
{
  "timestamp": "2026-08-31T12:00:00.000Z",
  "level": "INFO",
  "name": "app",
  "message": "function_c completed",
  "request_id": "a1b2c3d4-...",
  "user_id": "user1",
  "trace_id": "0af7651916cd43dd8448eb211c80319c",
  "span_id": "00f067aa0ba902b7"
}
```

**Fields:**

| Field | Source | Always Present |
|-------|--------|:--------------:|
| `timestamp` | `asctime` renamed | Yes |
| `level` | `levelname` renamed | Yes |
| `name` | Logger name | Yes |
| `message` | Log message | Yes |
| `request_id` | Passed via `extra` in `log()` | Yes |
| `user_id` | `UserIdFilter` via `ContextVar` | Yes (empty string if unset) |
| `trace_id` | `TraceContextFilter` or `log()` | Yes (all zeros if no span) |
| `span_id` | `TraceContextFilter` or `log()` | Yes (all zeros if no span) |
| `error` | `ErrorExtractionFilter` | Only on ERROR with exception |
| `elapsed_s` | Passed via `extra` in `log()` | Only on specific endpoints |

---

### 6. OpenTelemetry Tracing

**Provider:** `opentelemetry.sdk.trace.TracerProvider` (default, in-process only).

**Instrumentation:**
- `FastAPIInstrumentor.instrument_app(app)` — creates spans for each incoming HTTP request
- `HTTPXClientInstrumentor().instrument()` — creates spans for outgoing HTTPX requests

**Manual spans:**

```
start_endpoint (POST /start)
  ├── function_a
  └── function_b
        └── HTTP POST /process (auto-instrumented)
              └── process_endpoint
                    └── function_c
```

**Trace context propagation:** Handled automatically by OTel instrumentors via W3C Trace Context headers.

**Current limitation:** No exporter is configured. Traces exist only in-process and are not sent to any backend.

---

### 7. Log Aggregation Pipeline

#### Grafana Alloy

- **Discovery:** Docker service discovery via `/var/run/docker.sock`
- **Target:** All containers on the Docker host
- **Labels:** `container` (derived from Docker container name)
- **Destination:** Loki at `http://loki:3100/loki/api/v1/push`
- **Configuration:** `alloy/config.alloy`

#### Loki

- **Mode:** Single-instance, auth disabled
- **Storage:** Filesystem (chunks + rules)
- **Index:** TSDB with schema v13, 24h period
- **Ring:** In-memory (no clustering)

#### Grafana

- **Datasource:** Loki, auto-provisioned via `datasource.yml`
- **Access:** Proxy mode through Grafana backend

---

## Docker Infrastructure

| Service | Image | Port | Purpose |
|---------|-------|------|---------|
| `api` | Custom (Python 3.12-slim) | 8000 | FastAPI application |
| `mongodb` | `mongo:7` | 27017 | User data store |
| `loki` | `grafana/loki:latest` | 3100 | Log aggregation |
| `alloy` | `grafana/alloy:latest` | - | Log scraping |
| `grafana` | `grafana/grafana:latest` | 3000 | Visualization |

**Network:** All services on `observability-network`.

---

## Data Flow Summary

```
Request arrives
  │
  ▼
FastAPIInstrumentor creates root span
  │
  ▼
Auth dependency sets user_id in ContextVar
  │
  ▼
Endpoint handler creates manual span
  │
  ▼
Internal functions create child spans
  │
  ▼
log() helper reads OTel context → injects trace_id + span_id
  │
  ▼
Filters enrich LogRecord (user_id, trace context, error)
  │
  ▼
JsonFormatter serializes to JSON on stdout/stderr
  │
  ▼
Grafana Alloy scrapes Docker container logs
  │
  ▼
Loki stores and indexes logs
  │
  ▼
Grafana queries Loki for visualization
```

---

## Known Gaps

| Gap | Impact | Location |
|-----|--------|----------|
| `TraceContextFilter` not wired to app logger handler | Compensated by `log()` helper | `main.py:127` |
| No OTLP exporter configured | Traces not exported to any backend | `main.py:38` |
| No Tempo service | Traces not queryable in Grafana | `docker-compose.yml` |
| No OTLP collector | No trace pipeline | `docker-compose.yml` |
| `start_process()` has no span | Gap in trace hierarchy | `main.py:215` |
| No MongoDB operation spans | DB calls not traced | `main.py:227` |
| Grafana has no Tempo datasource | Cannot visualize traces | `datasource.yml` |
| No dashboard provisioning | Manual dashboard creation required | `grafana/provisioning/` |
