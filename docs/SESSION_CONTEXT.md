# Observability POC - Session Context

## Date: 2026-09-08 (updated 2026-09-24)

> **Note:** Sections 1–18 describe historical evolution of the POC. Some named pieces (e.g. `TraceContextFilter`, `ErrorExtractionFilter`, `logging_config.json`, direct `LokiHandler` push, custom tracing middleware) were later removed or replaced. **Current code state is described under “Current Architecture” and section 19 below.**

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

### 18. ErrorExtractionFilter `exc_info` Requirement
- **Problem**: `ErrorExtractionFilter` only activates when `record.exc_info` is truthy — but `logger.error()` does NOT set `exc_info` by default
- **Root cause**: `exc_info` is only auto-set by `logger.exception()`. For `logger.error()`, `logger.warning()`, etc., you must pass `exc_info=True` explicitly
- **Impact**: Without `exc_info=True`, the filter produces `"error": null` — exception details are silently lost
- **Correct usage**:
  ```python
  # WRONG — filter won't fire, record.error will be null:
  logger.error("request failed")

  # RIGHT — one of these:
  logger.error("request failed", exc_info=True)
  logger.exception("request failed")  # same as exc_info=True
  ```
- **Note**: Every Python exception inherits from `BaseException` — custom exception classes work fine with the filter. The activation condition is `exc_info`, not exception type.
- **Files changed**: `docs/SETUP.md` (troubleshooting section updated)

### 19. Baggage Lost on Unhandled Exception (Fix)
- **Problem**: On `/form_B` → httpx `ConnectTimeout` to `192.0.2.1:9999/unreachable`, the error log showed `user_id`, `org_id`, `form_id`, `form_record_id` as `<nil>` / empty while `trace_id` was still correct
- **Reproduced in Loki**:
  ```json
  {"message":"Unhandled exception","user_id":"<nil>","org_id":"<nil>","form_id":"<nil>","form_record_id":"<nil>"}
  ```
  vs earlier success/info logs on the same `trace_id` which had full baggage
- **Root cause**:
  - Baggage lives in OTel `contextvars` via `set_baggage` + `attach()` inside `require_auth` / endpoint
  - `form_B` had **no try/except** around the httpx call → exception bubbled to the single global `@app.exception_handler(Exception)`
  - By the time that handler runs, contextvars baggage is no longer in scope (async/task boundary — same class of issue as §11)
  - Handler called `logger.error(...)` **without** rebuilding baggage → `BaggageLogProcessor` saw empty context → body fields became `<nil>`
  - `trace_id` survived because the request **span** is still current (span context ≠ baggage layer)
- **Why only unhandled exceptions?**: Normal logs and caught errors (e.g. `/external` `except httpx.HTTPError: logger.error(...)` inside the endpoint) still run while baggage is attached
- **Fix (dual-write + safety net)**:
  1. **`baggage_middleware`**: body-derived baggage keys also written to `request.state` (durable copy)
  2. **`rebuild_baggage_from_request(request, fields=None)`** in `tracing.py`: for each field, `get_baggage(key) or request.state` → `set_baggage` → `attach()` so current context has baggage again
  3. **`unhandled_exception_handler`**: call `rebuild_baggage_from_request(request, fields=BAGGAGE_FIELDS)` **before** `logger.error(...)`
  4. **`/form_B`**: wrap httpx call in `try/except httpx.HTTPError` → `logger.error(..., exc_info=sys.exc_info())` **inside the endpoint** (baggage still live) → `HTTPException(502)` — matches `/external`, proper status code, fewer errors reach the global handler
- **Verified**: `/form_B` → HTTP 502; Loki error log:
  ```json
  {"message":"Upstream call failed","user_id":"alice","org_id":"acme","form_id":"Form B","form_record_id":"..."}
  ```
- **Mental model**: baggage = contextvars (lost at exception-handler boundary); `request.state` = plain object on Request (survives); fix = bridge `request.state` → `set_baggage` → `attach` → log
- **Files changed**: `main.py` (middleware `setattr`, exception handler, form_B try/except), `tracing.py` (`rebuild_baggage_from_request`, baggage imports)

## Current Architecture

```
App ──OTLP/gRPC──→ otel-collector ──OTLP──→ Tempo (traces)
                              └──OTLP──→ Loki (logs; body rewritten with baggage fields)
App ──/metrics──→ Prometheus (metrics)   [via collector prometheus exporter :8889]
Grafana → Loki + Tempo + Prometheus (dashboards)
```

**Current instrumentation (code as of 2026-09-24):**
- `FastAPIInstrumentor.instrument_app` — request spans
- `HTTPXClientInstrumentor` / `PymongoInstrumentor` — outbound spans
- `BaggageSpanProcessor` / `BaggageLogProcessor` — copy context baggage → span/log attributes
- `LoggingHandler` (OTLP) — app logs → collector → Loki (no `LokiHandler` / `python-logging-loki` path in current `tracing.py`)
- otel-collector `transform/logs` — embeds `user_id`, `org_id`, `form_id`, `form_record_id` into log **body** JSON
- No `TraceContextFilter` / `ErrorExtractionFilter` / `logging_config.json` / custom `tracing_middleware` in current code

## Services (docker compose)

Start with env file (no root `.env` in repo):

```bash
docker compose --env-file .env.uat up -d --build api
```

| Service | Port | Purpose |
|---------|------|---------|
| api | 8000 | FastAPI app |
| mongodb | 27017 | Database |
| otel-collector | 4317/4318 (internal), 8889 | OTLP in, metrics out |
| loki | 3100 | Log aggregation |
| tempo | 3200, 4317, 4318 | Trace backend |
| prometheus | 9090 | Metrics backend |
| grafana | 3000 | Dashboard UI |
| loadgen | — | Traffic generator |

## Files Modified/Created

### Modified
- `requirements.txt` - OTel SDK/exporters/instrumentations, motor, PyJWT, etc. (no `python-logging-loki` in current requirements)
- `docker-compose.yml` - api, mongodb, otel-collector, prometheus, loki, tempo, grafana, loadgen
- `tracing.py` - `init_telemetry` (TracerProvider/LoggerProvider/MeterProvider + Baggage processors + HTTPX/Pymongo instrumentors), `BAGGAGE_FIELDS`, `rebuild_baggage_from_request`, `@traced`
- `main.py` - `baggage_middleware` (body → baggage + `request.state`), `require_auth` dual-write, endpoints dual-write, global exception handler rebuilds baggage, `/form_B` httpx try/except → 502
- `otel-collector-config.yaml` - OTLP receivers; logs transform embeds baggage fields into body; exports to tempo/loki/prometheus
- `loki/loki-config.yml` - filesystem store, `allow_structured_metadata: true` (no host volume mount yet for `/loki` data)
- `grafana/provisioning/datasources/datasource.yml` - Loki + Tempo + Prometheus
- `grafana/provisioning/dashboards/json/internal_dashboard.json` - single-line logs panel + adhoc Filters variable

### Created
- `tempo/tempo-config.yml` - Tempo configuration
- `prometheus/prometheus.yml` - Prometheus scrape config
- `grafana/provisioning/dashboards/dashboard.yml` - Dashboard provider config
- `docs/SESSION_CONTEXT.md` - This file

## Environment Variables

Loaded via `--env-file .env.uat` (or `.env.prod`); no root `.env` committed.

| Variable | Example (uat) | Purpose |
|----------|-------|---------|
| APP_ENV | uat | Environment tag |
| BAGGAGE_FIELDS | `["user_id","org_id","form_id","form_record_id"]` | Keys middleware copies body → baggage |
| JWT_SECRET_KEY | change-me-uat | JWT signing key |
| MONGO_URL | mongodb://mongodb:27017 | MongoDB connection |
| OTEL_EXPORTER_OTLP_ENDPOINT | http://otel-collector:4317 | OTLP gRPC (traces/logs/metrics from app) |
| TEMPO_ENDPOINT | tempo:4317 | Collector → Tempo |
| LOKI_OTLP_ENDPOINT | http://loki:3100/otlp | Collector → Loki OTLP |
| API_URL | http://api:8000 | loadgen target |

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
- **Status**: Historical — `ErrorExtractionFilter` is **not in current `tracing.py`**; no action unless that class is reintroduced
- **Problem**: Filter can crash silently on edge cases, removing itself from the logger for the rest of the session
- **Edge cases to fix**:
  1. `exc_tb is None` → `traceback.extract_tb(None)` raises `TypeError`
  2. `exc_value is None` → `exc_value.__cause__` raises `AttributeError`
  3. `exc_value.request is None` → `exc_value.request.url` raises `AttributeError`
- **Fix**: Guard all access with `if exc_tb`, `if exc_value`, `exc_value.request and hasattr(...)`
- **Also**: Check `__context__` fallback (for exceptions raised during handling, not just `raise X from Y`)
- **File**: `tracing.py` (`ErrorExtractionFilter` class) — **class currently absent**
- **Priority**: Low unless class returns — was High when filter was present

### Other open items (from this session’s investigation)
- **Loki local persistence**: `loki/loki-config.yml` uses container paths `/loki/chunks` but `docker-compose.yml` has **no volume** for `/loki` — data lives in container writable layer and is lost on recreate. Fix: mount e.g. `./loki/data:/loki`
- **Grafana adhoc Filters vs structured metadata**: Dashboard `AdhocVariable` injects filters into the **stream selector**; only `service_name` / `service_instance_id` are stream labels. `trace_id`, `user_id`, etc. are structured metadata — filter with LogQL `| key="value"` or Explore “Filter for value”, not the dashboard Filters control ([grafana-loki-datasource#155](https://github.com/grafana/grafana-loki-datasource/issues/155))
- **Docs drift**: `AGENTS.md`, `DESIGN.md`, `SETUP.md`, `FEATURE.md` still reference removed pieces (`TraceContextFilter`, `log_span`, `enrich_span_from_context`, `logging_config.json`, LokiHandler) — need a pass similar to this file

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
10. **Nested error object over flat fields** - Keeps logs smaller; query via LogQL `| json | label_format` at query time (historical if ErrorExtractionFilter returns)
11. **wrapLogMessage already enabled** - Dashboard logs panel has `wrapLogMessage: true` — logs display line by line; Explore view needs manual toggle
12. **Rebuild baggage in global exception handler** - contextvars baggage is lost at the unhandled-exception boundary; `request.state` is the durable copy; `rebuild_baggage_from_request` bridges it before logging
13. **Catch httpx errors inside endpoints when practical** - logs while baggage is still attached and return correct 5xx (502) instead of falling through to a generic 500
