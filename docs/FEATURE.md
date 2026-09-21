# Observability Feature — What, Why, and How

A summary of the observability layer built for our FastAPI services.

---

## 1. Executive Summary

We built a portable, non-invasive observability library (`tracing.py`) that adds structured logging and trace context correlation to any FastAPI service with minimal code changes.

**What it does:** Every HTTP request automatically gets a unique trace ID. Every log line carries that trace ID along with user context. All of this surfaces in Grafana dashboards — logs and trace context in one place.

**Business value:** When an incident occurs, engineers can go from "something is broken" to "this specific request for this specific user hit this specific error in this specific code path" in seconds instead of minutes. No more grepping plain text logs across multiple services. No more guessing which request caused the issue.

**Adoption cost:** ~30 minutes per service. Copy one file, add dependencies, set two environment variables, add ten lines of code. Zero changes to business logic.

---

## 2. The Problem

### Before

Our FastAPI services had three blind spots:

**1. Plain text logs with no structure.**
Logs went to Loki as unstructured text. You could search by keyword, but not by user, endpoint, error type, or trace. A single request that touched multiple services left scattered log lines with no way to connect them.

**2. No correlation between logs and traces.**
If a dashboard showed errors, you had to manually cross-reference timestamps between Loki logs to find the cause. There was no shared identifier linking a specific log line to the full request lifecycle.

**3. Adding observability was a custom effort per service.**
Each service had its own logging setup, its own middleware, its own way of handling errors. There was no standard. Adding observability to a new service meant days of wiring up log handlers, creating dashboards, and writing middleware — work that was repeated differently every time.

### The Impact

- **Incident response was slow.** Finding the root cause of a production issue required manually correlating timestamps across logs.
- **Debugging was painful.** A user reports "I got an error" — you have no way to filter logs by that user, trace that request, or see what happened end-to-end.
- **Inconsistency across services.** Each team implemented logging differently. Some services had structured logs, some didn't. There was no uniform observability baseline.

---

## 3. What We Built

### The Core: `tracing.py`

A single, self-contained Python file (~250 lines) that provides:

**Structured JSON logging** — Every log line is a structured JSON object with fields like `trace_id`, `span_id`, `user_id`, `org_id`, `level`, `message`, and `timestamp`. Loki can query any of these fields without custom parsing rules.

**Trace context propagation** — Every HTTP request gets a unique OpenTelemetry trace ID. Every log line written during that request carries the trace ID. This means you can go from a log line to its full trace, or from a trace to all its log lines, with a single query.

**Baggage propagation** — User context (`user_id`, `org_id`, and custom fields) flows through the entire request lifecycle via OpenTelemetry baggage. Every log line and every span automatically includes these fields. No need to pass them manually between functions.

**Error enrichment** — On ERROR+ log calls, exception details are extracted into a structured `error` field with type, message, code location, and stack trace. No more parsing stack traces from plain text.

**Grafana dashboards** — Pre-built dashboards for log viewing with ad-hoc filters (trace_id, user_id, endpoint) and log volume over time.

### The Integration Model

The library is designed around a simple principle: **observability wraps around your code, not inside it.**

- `instrument_app(app)` — auto-instruments FastAPI (one line)
- `setup()` — initializes logging, Loki handler, trace context filter (one line)
- `tracing_middleware` — enriches spans with HTTP attributes, logs span completion (copy once)
- `set_baggage()` — propagate user context through the request (add in auth)

Your endpoints remain untouched. The middleware and filters handle the heavy lifting automatically.

---

## 4. Architecture

```
                    ┌─────────────────────────────────────┐
                    │           Your FastAPI App           │
                    │                                     │
                    │  ┌──────────┐    ┌──────────────┐  │
  HTTP Request ────▶│  │ FastAPI  │───▶│   Your       │  │
                    │  │ Auto-    │    │   Endpoints  │  │
                    │  │ Instru-  │    │              │  │
                    │  │ mentor   │    │  set_baggage │  │
                    │  └────┬─────┘    └──────┬───────┘  │
                    │       │                 │           │
                    │       ▼                 ▼           │
                    │  ┌─────────────────────────────┐   │
                    │  │     Tracing Middleware       │   │
                    │  │  - set HTTP attributes       │   │
                    │  │  - enrich from baggage       │   │
                    │  │  - log span completion       │   │
                    │  └──────────────┬──────────────┘   │
                    │                 │                   │
                    │                 ▼                   │
                    │          ┌──────────┐              │
                    │          │ Loki     │              │
                    │          │ Handler  │              │
                    │          └────┬─────┘              │
                    └───────────────┼────────────────────┘
                                    │
                                    ▼
                            ┌──────────────┐
                            │    Loki      │
                            │ (structured  │
                            │  JSON logs)  │
                            └──────┬───────┘
                                   │
                                   ▼
                            ┌──────────────┐
                            │   Grafana    │
                            │              │
                            │ ┌──────────┐ │
                            │ │ Log      │ │
                            │ │ Volume   │ │
                            │ └──────────┘ │
                            │ ┌──────────┐ │
                            │ │ Logs     │ │
                            │ │ (filtered│ │
                            │ │  by user,│ │
                            │ │  trace)  │ │
                            │ └──────────┘ │
                            └──────────────┘
```

### Data Flow

1. **Request arrives** → FastAPI auto-instrumentor creates a span with a unique trace ID
2. **Middleware runs** → sets `http.method`, `http.url`, `http.route` on the span
3. **Auth dependency runs** → sets `user_id` and `org_id` via OpenTelemetry baggage
4. **BaggageSpanProcessor** → automatically copies baggage fields into span attributes
5. **Your endpoint logs** → `TraceContextFilter` injects `trace_id`, `span_id`, `user_id`, `org_id` into every log record
6. **LokiHandler** → pushes structured JSON log to Loki
7. **Middleware completes** → logs span completion via `log_span()` (another structured log entry)

### Correlation

Every log line has a `trace_id`. Every span has a `trace_id`.

- **Log → Trace:** Filter Loki by `trace_id="abc123..."` to find all logs for one request
- **Trace → Logs:** In Grafana Tempo, click a span to see its associated log lines
- **User → Everything:** Filter by `user_id="alice"` to see all logs and traces for one user

---

## 5. Key Design Decisions

### Why OpenTelemetry?

OpenTelemetry is the CNCF standard for observability. It's vendor-neutral — the same instrumentation works with Loki, Datadog, Grafana Cloud, or any OTLP-compatible backend. If we switch backends later, we don't rewrite instrumentation. We just change the exporter endpoint.

### Why structured JSON logs?

Plain text logs require Promtail parsing rules to extract fields. Every new field means updating Promtail config, redeploying, and hoping you didn't break existing queries. Structured JSON logs are self-describing — any field is immediately queryable in Loki without parser configuration. The `TraceContextFilter` auto-injects fields, so every log line has `trace_id`, `user_id`, etc. without any manual work.

### Why non-invasive?

Business logic should not know about observability. Adding `@traced` to every function couples your domain code to your monitoring infrastructure. Instead, the middleware and auto-instrumentors handle span creation, attribute setting, and log enrichment. Your endpoints stay clean. The only observability code in your endpoints is `set_baggage()` calls in auth — and even that is a thin wrapper around context propagation.

### Why a single file?

Deployment simplicity. No package to publish, no versioning, no dependency management. Copy `tracing.py` into your project, and it works. If we improve it, you pull the latest version. If you need to customize it, you edit the file directly. This is a deliberate trade-off against reusability in favor of adoption speed.

### Why BaggageSpanProcessor?

OpenTelemetry baggage is designed for cross-service context propagation, but it doesn't automatically become span attributes. `BaggageSpanProcessor` bridges this gap — it copies every baggage field into the current span's attributes. This means `user_id` and `org_id` set in your auth dependency automatically appear in every span and every log line without manual wiring.

---

## 6. How It Works

### The Request Lifecycle

When an HTTP request hits a FastAPI endpoint instrumented with this library, the following happens:

**Phase 1: Span Creation**
FastAPI's auto-instrumentor detects the incoming request and creates a new OpenTelemetry span. The span gets a unique trace ID and span ID. This span represents the entire HTTP request lifecycle.

**Phase 2: Attribute Enrichment**
The tracing middleware sets standard HTTP attributes on the span: `http.method` (GET, POST, etc.), `http.url` (full request URL), and `http.route` (the URL pattern, e.g., `/users/{id}`). These attributes make spans searchable in trace backends.

**Phase 3: Context Propagation**
Your auth dependency decodes the JWT and calls `set_baggage("user_id", user_id)`. The `BaggageSpanProcessor` detects this and copies `user_id` into the span's attributes. Now every subsequent log line and span in this request carries the user identity.

**Phase 4: Business Logic**
Your endpoint code runs. Every `logger.info("...")` call passes through `TraceContextFilter`, which reads the current span's `trace_id` and `span_id` and injects them into the log record. The `LokiHandler` formats the log as structured JSON and pushes it to Loki. The log line now has `trace_id`, `span_id`, `user_id`, `org_id` — all auto-injected, none manually added.

**Phase 5: Span Completion**
After the response is sent, the middleware calls `log_span()`, which emits a structured `span_completed` log entry to Loki. This log includes the span name, duration in milliseconds, status (OK/ERROR), and all HTTP attributes. This gives you a structured record of every request's timing and outcome.

### What Each Component Does

**`instrument_app(app)`** — Calls `FastAPIInstrumentor.instrument_app()`. This hooks into FastAPI's request/response cycle and creates spans automatically. Excludes `/health` and `/metrics` endpoints to avoid noise.

**`setup()`** — Calls `HTTPXClientInstrumentor().instrument()` to trace outbound HTTP calls. Creates a `LokiHandler` (if `LOKI_URL` is set) that pushes structured JSON logs to Loki. Adds `TraceContextFilter` to inject trace context into every log record. Returns a configured logger.

**`TraceContextFilter`** — A logging filter that reads the current OpenTelemetry span's `trace_id` and `span_id`, reads baggage fields (`user_id`, `org_id`, `form_id`, `form_record_id`), and injects them into every log record as extra fields. This is what makes log-trace correlation work.

**`ErrorExtractionFilter`** — A logging filter that activates on ERROR+ log calls. It extracts exception details (type, message, code location, stack trace) into a structured `error` JSON field. It also cleans up Python's `^^^` indicator lines from stack traces. This makes error logs machine-readable and queryable in Loki.

**`LokiHandler`** — Sends log records to Loki's push API as structured JSON. Each log line includes the `service_name` tag (configurable via `SERVICE_NAME` env var) and all fields from `TraceContextFilter`.

**`log_span(span, span_name, ...)`** — Emits a structured `span_completed` log entry to Loki with the span's name, duration, status, trace ID, span ID, and HTTP attributes. This gives you a structured record of every request's timing and outcome without needing Tempo.

**`enrich_span_from_context(span, request)`** — Reads baggage fields from the OpenTelemetry context (with `request.state` as fallback), sets them as span attributes, and re-attaches the baggage to the current context. This ensures baggage propagates through the entire request.

**`@traced(span_name, skip_paths)`** — A decorator that creates a child span, sets status on success/error, and logs span completion via `log_span()`. Optional `skip_paths` parameter allows skipping tracing for specific endpoints.

**`BAGGAGE_FIELDS`** — A list of field names that `enrich_span_from_context` reads from baggage. Default: `["user_id", "org_id", "form_id", "form_record_id"]`. Customize this to match your application's context fields.

### Log Structure in Loki

Every log line in Loki is a structured JSON object:

```json
{
  "timestamp": "2025-01-15T10:30:00.000Z",
  "level": "INFO",
  "name": "app",
  "message": "POST /submit_order completed",
  "trace_id": "abc123def45678901234567890123456",
  "span_id": "7890123456789012",
  "user_id": "alice",
  "org_id": "acme",
  "form_id": "order-form",
  "form_record_id": "ord-123"
}
```

Error logs include a structured `error` field:

```json
{
  "timestamp": "2025-01-15T10:30:00.000Z",
  "level": "ERROR",
  "name": "app",
  "message": "Database connection failed (url=postgres://db:5432/mydb)",
  "trace_id": "abc123def45678901234567890123456",
  "span_id": "7890123456789012",
  "user_id": "alice",
  "org_id": "acme",
  "error": {
    "type": "ConnectionFailure",
    "message": "Connection refused (url=postgres://db:5432/mydb)",
    "location": "/app/services/database.py:42 in connect",
    "stack_trace": ["File \"/app/services/database.py\", line 42, in connect"],
    "stack_trace_full": ["Traceback (most recent call last)..."]
  }
}
```

---

## 7. What's Included

### Deliverables

| File | Purpose |
|------|---------|
| `tracing.py` | Core observability library — tracer setup, instrumentors, logging filters, span helpers, `@traced` decorator. ~250 lines, self-contained. |
| `logging_config.json` | Python `dictConfig` for structured JSON logging to console (stderr/stdout) with `ErrorExtractionFilter`. |
| `grafana/provisioning/dashboards/json/internal_dashboard.json` | Grafana v2 dashboard — single log panel with ad-hoc variable filters for trace_id, user_id, endpoint, level. |
| `grafana/provisioning/dashboards/json/internal_dashboard_wrapped.json` | Same dashboard with wrapped log lines for better readability. |
| `grafana/provisioning/dashboards/disabled/observability-api.json` | Full Grafana v1 dashboard — Log Volume, Logs, Trace Search. |
| `SETUP.md` | Detailed integration guide — step-by-step instructions for adding observability to any FastAPI service. |

### What Each Deliverable Provides

**`tracing.py`** is the only code file you need to copy. It handles:
- OpenTelemetry tracer initialization
- FastAPI and HTTPX auto-instrumentation
- Structured JSON logging to Loki
- Trace context injection into every log line
- Baggage propagation for user context
- Error extraction into structured fields
- Span metadata logging

**`logging_config.json`** configures Python's logging system to output structured JSON to the console. It includes the `ErrorExtractionFilter` for structured error output. This file is used by `uvicorn`'s `--log-config` option.

**The dashboard JSON files** are Grafana-provisionable dashboards. The `internal_dashboard.json` files are in Grafana v2 K8s resource format for file-based provisioning. The `observability-api.json` is in classic format for UI import.

**`SETUP.md`** covers the full integration process: file placement, dependency installation, environment variables, code changes, dashboard configuration, and troubleshooting. It's designed so that any engineer can add observability to a service in ~30 minutes.

### What You Need to Provide

| Requirement | Purpose |
|-------------|---------|
| Loki instance | Stores structured JSON logs |
| Grafana instance | Visualizes logs |
| FastAPI application | The service you want to instrument |

Optional:
| Requirement | Purpose |
|-------------|---------|
| Tempo instance | Distributed trace visualization (logs work without it) |
