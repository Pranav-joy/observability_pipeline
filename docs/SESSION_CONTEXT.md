# Observability POC - Session Context

## Date: 2026-09-08 (updated 2026-09-11)

## What We Achieved

### 1. Tracing Middleware Refactor
- **Problem**: Tracing was done via `@traced` decorator on individual endpoints, not in middleware layer
- **Solution**: Moved tracing to middleware layer using `@traced` decorator with callable span name
- **Result**: Root span created per request in middleware, covers entire request lifecycle

### 2. Switched to FastAPI Instrumentor (then reverted)
- **Explored**: Using `opentelemetry-instrumentation-fastapi` for automatic instrumentation
- **Decision**: Reverted to custom middleware approach (production code doesn't use FastAPI instrumentor)
- **Final**: Custom middleware with `@traced("HTTP")` fixed span name

### 3. Database Layer for Form Records
- **Added**: MongoDB `form_records` collection for storing form submissions
- **form_A**: Always inserts record on success
- **form_B**: 50% random rejection before DB insert
- **Document structure**: `{message, form_record_id, user_id}`

### 4. Form Record ID in Logs
- **Added**: `extra={"form_record_id": form_record_id}` to relevant log lines
- **Scope**: Only form-related logs include this field (not global filter)
- **Updated**: Now auto-injected via TraceContextFilter from baggage (no manual `extra={}` needed)

### 5. Removed Alloy - Direct Loki Logging
- **Problem**: Alloy was an unnecessary中间层 for log forwarding
- **Solution**: App pushes logs directly to Loki via `python-logging-loki`
- **Changes**:
  - Added `python-logging-loki` to requirements.txt
  - Updated `tracing.py` `setup()` to create `LokiHandler` when `LOKI_URL` env var is set
  - Removed `alloy` service from docker-compose.yml
  - Added `LOKI_URL=http://loki:3100` to api environment

### 6. Auto-Provisioned Grafana Dashboard
- **Created**: `grafana/provisioning/dashboards/dashboard.yml` (provider config)
- **Created**: `grafana/provisioning/dashboards/json/observability-api.json`
  - Log Volume panel (timeseries bar chart)
  - Logs panel (live log stream with level filter, `wrapLogMessage: true` for line-by-line view)
  - Traces panel (TraceQL search)
  - Query: `{service_name="observability-api-1"}`
- **Dashboard auto-appears** on Grafana startup
- **Note**: Logs panel wraps lines by default; Grafana Explore view requires manual toggle (bottom-left corner or `Ctrl+Shift+U`)

### 7. Tempo Trace Export (Full Instrumentation - Traces)
- **Added**: `opentelemetry-exporter-otlp-proto-grpc` to requirements.txt
- **Created**: `tempo/tempo-config.yml` (OTLP receiver, local storage)
- **Updated**: `docker-compose.yml` with `tempo` service
- **Updated**: `tracing.py` with:
  - `Resource` with `service.name=observability-api-1`
  - `BatchSpanProcessor` + `OTLPSpanExporter` (when `OTEL_EXPORTER_OTLP_ENDPOINT` is set)
- **Updated**: Grafana datasource provisioning with Tempo
- **Updated**: Dashboard with Trace Search panel

### 8. Prometheus Metrics
- **Added**: `prometheus_client` to requirements.txt
- **Created**: `prometheus/prometheus.yml` scrape config
- **Added**: Prometheus service to docker-compose.yml (port 9090)
- **Added**: Prometheus datasource to Grafana
- **Added**: `/metrics` endpoint to FastAPI app
- **Added**: Metrics panels to Grafana dashboard (Request Rate + Request Duration p50/p95)
- **Updated**: `@traced` decorator in tracing.py to record:
  - `app_requests_total` counter (labels: span_name, status)
  - `app_request_duration_seconds` histogram (label: span_name)

### 9. Auto-Injected Log Context (form_record_id, form_id)
- **Updated**: `TraceContextFilter` in tracing.py to auto-inject `form_record_id` and `form_id` from baggage
- **Updated**: form_A and form_B endpoints to set `form_id` in baggage
- **Result**: Removed all manual `extra={}` from log calls — filter handles it automatically
- **Benefit**: Cleaner code, every log line automatically gets full context

### 10. Duplicate Log Fix
- **Problem**: Same log line appearing twice in console output
- **Root cause**: `dictConfig` (from `--log-config`) was adding a handler to the root logger as a side effect, and "app" logger propagated to it
- **Fix**: Added `"root": {"handlers": [], "level": "WARNING", "propagate": false}` to `logging_config.json`
- **Also**: Removed StreamHandler from `setup()` — console output now handled solely by `dictConfig`
- **Result**: Single source of truth for console logging, no duplicates

### 11. Parent Span Baggage Fix (request.state Fallback)
- **Problem**: Parent span logs showed empty `user_id`, `org_id`, `form_id`, `form_record_id` despite baggage being set in endpoint handlers
- **Root cause**: OTel context doesn't propagate back from endpoint handlers to middleware in async FastAPI. `get_baggage()` returns empty in middleware after `call_next()` for endpoints that set baggage internally (e.g., `/signin`, `/form_A`, `/form_B`)
- **Fix**:
  - Added `request.state.user_id` and `request.state.org_id` to `/signin` endpoint alongside baggage
  - Added `request.state.form_id` and `request.state.form_record_id` to `/form_A` and `/form_B` endpoints
  - Middleware falls back to `request.state` when `get_baggage()` returns empty
  - Middleware re-attaches baggage context before `log_span()` so `TraceContextFilter` picks up values
- **Why `request.state`?**: It's a plain attribute on the FastAPI `Request` object, passed by reference. No context propagation needed — immune to asyncio task boundary issues that affect OTel context variables.
- **Files changed**: `main.py` (middleware + endpoints)

### 12. enrich_span_from_context Helper
- **Problem**: Middleware had ~20 lines of duplicated baggage resolution logic in both success and exception paths
- **Solution**: Created `enrich_span_from_context(span, request)` helper in `tracing.py`
- **What it does**:
  1. Iterates over `BAGGAGE_FIELDS` (`user_id`, `org_id`, `form_id`, `form_record_id`)
  2. For each field: `get_baggage(field) or getattr(request.state, field, '') or ""`
  3. Sets resolved values as span attributes
  4. Re-attaches baggage context for `TraceContextFilter`
- **Middleware simplified**: Both success and exception paths now call `enrich_span_from_context(span, request)` — one line each
- **Files changed**: `tracing.py` (new helper), `main.py` (middleware simplified, imports updated)

### 13. ErrorExtractionFilter Duplicate Fix
- **Problem**: `ErrorExtractionFilter` wasn't populating the `error` field in ERROR logs — output showed `"error": null`
- **Root cause**: Two `ErrorExtractionFilter` instances running on the same `LogRecord`:
  1. Logger-level filter (from `setup()` in `tracing.py`)
  2. Handler-level filter (from `logging_config.json` on `"default"` and `"access"` handlers)
- **Sequence**:
  1. Logger-level filter runs → extracts error details into `record.error` → clears `record.exc_info = None`
  2. Handler-level filter runs → sees `record.exc_info` is `None` → overwrites `record.error = null`
- **Fix**: Removed `ErrorExtractionFilter` from `setup()` — only the handler-level filter from `logging_config.json` remains
- **Result**: ERROR logs now show full error details:
  ```json
  "error": {
    "type": "ServerSelectionTimeoutError",
    "message": "mongodb:27017: [Errno -2] Name or service not known ...",
    "location": "/app/db.py:32 in insert_form_record",
    "stack_trace": "..."
  }
  ```
- **Why keep handler-level?**: It covers all loggers (app, uvicorn, uvicorn.access) via `logging_config.json`, not just the app logger
- **Files changed**: `tracing.py` (removed redundant filter from `setup()`)

### 14. ErrorExtractionFilter Enhancement (location + message)
- **Problem 1**: `location` field showed library code (e.g., `httpx/_transports/default.py`) instead of app code
- **Fix 1**: Find last frame with `/app/` in path for `location` field (closest to exception origin); fallback to last frame if no app frames
- **Problem 2**: `location` showed middleware frame instead of actual error source
- **Fix 2**: Use `app_frames[-1]` (last app frame) instead of `app_frames[0]` (first app frame) — last frame is closest to where the exception actually happened
- **Problem 3**: `message` field empty for httpx `ConnectTimeout` (exception raised without message string)
- **Fix 3**: 
  - Fall back to `__cause__` message if primary exception message is empty
  - For httpx exceptions with `request.url`, include URL in message
- **Before**:
  ```json
  "error": {
    "type": "ConnectTimeout",
    "message": "",
    "location": "/app/main.py:140 in tracing_middleware"
  }
  ```
- **After**:
  ```json
  "error": {
    "type": "ConnectTimeout",
    "message": "request to http://192.0.2.1:9999/unreachable failed",
    "location": "/app/main.py:242 in form_B"
  }
  ```
- **Files changed**: `tracing.py` (`ErrorExtractionFilter` class enhanced)

### 15. Stack Trace Line-by-Line + Split Fields
- **Problem**: `stack_trace` was a single joined string — all lines collapsed in Grafana logs
- **Fix**: Changed `stack_trace` to a JSON array (one element per traceback line) instead of `"".join(...)`
- **Also**: Split into two fields:
  - `stack_trace` — app frames only (filtered to `/app/` paths), no library/venv noise
  - `stack_trace_full` — complete traceback (original, all frames)
- **Files changed**: `tracing.py` (`ErrorExtractionFilter.filter`)

### 16. Caret (`^^^^`) Removal from Stack Traces
- **Problem**: Python 3.11+ adds `^^^^^^^^^^^^^^^^` caret lines to tracebacks — noisy in logs
- **Fix**: Created `_clean_trace()` static method on `ErrorExtractionFilter`
  - Splits each formatted entry by `\n`, filters out lines that are all `^` characters
  - Strips trailing `\n` from each entry
- **Applied to**: Both `stack_trace` (app frames) and `stack_trace_full` (full trace)
- **Files changed**: `tracing.py` (`ErrorExtractionFilter._clean_trace` static method)

### 17. Querying Nested Error Fields in Loki (LogQL)
- **Problem**: `error.type` is a nested JSON object — can't query directly with LogQL label matchers
- **Decision**: Keep nested structure (smaller logs), use LogQL parser at query time
- **Query pattern**:
  ```logql
  {service_name="observability-api-1"} | json | label_format error_type="{{error.type}}" | error_type="ConnectTimeout"
  ```
- **How it works**: `| json` flattens nested fields, `| label_format` extracts into queryable labels
- **No code changes** — this is a Grafana/LogQL concern only

## Current Architecture

```
App ──OTLP──→ Tempo (traces)
App ──HTTP──→ Loki (logs)
App ──/metrics──→ Prometheus (metrics)
Grafana → Loki + Tempo + Prometheus (dashboards)
```

## Services (docker-compose up)

| Service | Port | Purpose |
|---------|------|---------|
| api | 8000 | FastAPI app |
| mongodb | 27017 | Database |
| loki | 3100 | Log aggregation |
| tempo | 3200, 4317, 4318 | Trace backend |
| prometheus | 9090 | Metrics backend |
| grafana | 3000 | Dashboard UI |

## Files Modified/Created

### Modified
- `requirements.txt` - Added python-logging-loki, opentelemetry-exporter-otlp-proto-grpc, prometheus_client
- `docker-compose.yml` - Removed alloy, added tempo + prometheus, added environment variables
- `tracing.py` - Added LokiHandler, OTLP exporter, Resource, Prometheus metrics, form context in filter, removed StreamHandler, added `enrich_span_from_context` helper, removed duplicate ErrorExtractionFilter, split stack_trace into app-only + full, added `_clean_trace()` for caret removal
- `main.py` - Added /metrics endpoint, form_id in baggage, removed extra={} from logs, added request.state fallback in middleware, simplified middleware with enrich_span_from_context, added request.state to /signin, /form_A, /form_B
- `logging_config.json` - Added app logger, root logger config (duplicate fix), error_extraction filter
- `grafana/provisioning/datasources/datasource.yml` - Added Tempo + Prometheus datasources
- `grafana/provisioning/dashboards/json/observability-api.json` - Full dashboard with logs + traces + metrics

### Created
- `tempo/tempo-config.yml` - Tempo configuration
- `prometheus/prometheus.yml` - Prometheus scrape config
- `grafana/provisioning/dashboards/dashboard.yml` - Dashboard provider config
- `SESSION_CONTEXT.md` - This file

## Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| LOKI_URL | http://loki:3100 | Loki endpoint for log export |
| OTEL_EXPORTER_OTLP_ENDPOINT | http://tempo:4317 | Tempo endpoint for trace export |
| MONGO_URL | mongodb://mongodb:27017 | MongoDB connection |
| JWT_SECRET_KEY | dev-secret-change-in-prod | JWT signing key |

## Pending / Future Work

### Annotation Library (from senior dev's PDF)
- Wrap DB calls with `@traced` decorators for per-DB-call spans
- Wrap external API calls with `@traced` for per-external-call spans
- Extend `@traced` to capture DB operation count, external API call count
- Document generator instrumentation

### Custom Log Shipper
- Senior dev has a custom script for log shipping
- Can replace python-logging-loki when ready
- Both achieve the same result (push logs to Loki HTTP API)

### ErrorExtractionFilter Robustness Fixes
- **Problem**: Filter can crash silently on edge cases, removing itself from the logger for the rest of the session
- **Edge cases to fix**:
  1. `exc_tb is None` → `traceback.extract_tb(None)` raises `TypeError`
  2. `exc_value is None` → `exc_value.__cause__` raises `AttributeError`
  3. `exc_value.request is None` → `exc_value.request.url` raises `AttributeError`
- **Fix**: Guard all access with `if exc_tb`, `if exc_value`, `exc_value.request and hasattr(...)`
- **Also**: Check `__context__` fallback (for exceptions raised during handling, not just `raise X from Y`)
- **File**: `tracing.py` (`ErrorExtractionFilter` class)
- **Priority**: High — silent filter removal breaks error extraction for all subsequent logs

## Key Decisions

1. **Custom middleware over FastAPI instrumentor** - Production code uses custom approach
2. **Direct Loki push over Alloy** - Simpler architecture, one less service
3. **Traces first, metrics later** - Incremental approach to full observability
4. **Infra + exporters scope** - Minimal app code changes, focus on backend setup
5. **dictConfig for console logging** - Single source of truth, setup() only handles Loki + filters
6. **Baggage-based log context** - form_record_id, form_id auto-injected via filter, no manual extra={}
7. **request.state as baggage fallback** - OTel context doesn't propagate back from endpoint handlers to middleware in async FastAPI; request.state is immune to this
8. **Single ErrorExtractionFilter on handlers** - Avoids duplicate filter overwriting record.error; handler-level filter covers all loggers
9. **Split stack_trace into app-only + full** - `stack_trace` = filtered app frames for quick scanning; `stack_trace_full` = complete trace for deep debugging
10. **Nested error object over flat fields** - Keeps logs smaller; query via LogQL `| json | label_format` at query time
11. **wrapLogMessage already enabled** - Dashboard logs panel has `wrapLogMessage: true` — logs display line by line; Explore view needs manual toggle
