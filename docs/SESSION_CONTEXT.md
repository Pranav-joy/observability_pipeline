# Observability POC - Session Context

## Date: 2026-09-08 (updated 2026-09-09)

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
  - Logs panel (live log stream with level filter)
  - Traces panel (TraceQL search)
  - Query: `{service_name="observability-api-1"}`
- **Dashboard auto-appears** on Grafana startup

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
- `tracing.py` - Added LokiHandler, OTLP exporter, Resource, Prometheus metrics, form context in filter, removed StreamHandler
- `main.py` - Added /metrics endpoint, form_id in baggage, removed extra={} from logs
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

## Key Decisions

1. **Custom middleware over FastAPI instrumentor** - Production code uses custom approach
2. **Direct Loki push over Alloy** - Simpler architecture, one less service
3. **Traces first, metrics later** - Incremental approach to full observability
4. **Infra + exporters scope** - Minimal app code changes, focus on backend setup
5. **dictConfig for console logging** - Single source of truth, setup() only handles Loki + filters
6. **Baggage-based log context** - form_record_id, form_id auto-injected via filter, no manual extra={}
