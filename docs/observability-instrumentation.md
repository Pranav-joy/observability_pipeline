# Observability POC — Instrumentation & Annotations Deep Dive

## Architecture Overview

```
App ──OTLP──→ Tempo (traces)
App ──HTTP──→ Loki (logs)
App ──/metrics──→ Prometheus (metrics)
Grafana → Loki + Tempo + Prometheus (dashboards)
```

## What's Instrumented

### 1. Traces (OpenTelemetry → Tempo)

#### Span Hierarchy
```
http_request (root span - middleware)
├── db.search_user          (signin endpoint)
├── db.insert_form_record   (form_A / form_B)
├── db.count_users          (startup)
├── db.seed_users           (startup)
└── httpx GET /dummy        (auto-instrumented outbound call)
```

#### Manual Spans via @traced Decorator
- `tracing.py` defines a `@traced` decorator that wraps async functions
- Creates a child span, sets user_id/org_id from baggage, records status, logs duration
- Supports `skip_paths` parameter to skip tracing for specific paths (e.g., /metrics, /health)

#### Auto-Instrumented
- **httpx** — `HTTPXClientInstrumentor().instrument()` auto-creates outbound HTTP spans

#### Span Attributes (set on http_request span)
| Attribute | Source |
|-----------|--------|
| `http.method` | Middleware |
| `http.url` | Middleware |
| `http.route` | Middleware |
| `http.status_code` | Middleware |
| `user_id` | @traced decorator (from baggage) |
| `org_id` | @traced decorator (from baggage) |

#### Baggage Context Propagation
| Field | Set Where | Flows To |
|-------|-----------|----------|
| `user_id` | `signin`, `require_auth` | Logs + spans |
| `org_id` | `signin`, `require_auth` | Logs + spans |
| `form_record_id` | `form_A`, `form_B` | Logs |
| `form_id` | `form_A`, `form_B` | Logs |

#### Span Status
- OK — request completed successfully
- ERROR — exception raised before response returned

---

### 2. Logs (Python Logging → Loki)

#### Structured JSON Logging
- All app logs are structured JSON via `pythonjsonlogger`
- Format: `{timestamp, level, name, message, trace_id, span_id, user_id, org_id, form_record_id, form_id}`

#### Logging Filters
| Filter | Purpose |
|--------|---------|
| `TraceContextFilter` | Auto-injects `trace_id`, `span_id`, `user_id`, `org_id`, `form_record_id`, `form_id` into every log record |
| `ErrorExtractionFilter` | Extracts exception type, message, file location, stack trace into structured `error` field for ERROR+ logs |

#### Log Shipping
- `python-logging-loki` pushes logs directly to Loki via `LokiHandler`
- Service label: `observability-api-1` (matches Grafana dashboard queries)

#### Logger Configuration
- `logging_config.json` handles console output (dictConfig via uvicorn)
- `tracing.py setup()` handles LokiHandler + filters
- `setup()` is idempotent — safe to call before or after dictConfig

#### Log Suppression
- `uvicorn.access` set to `WARNING` level — no more noise from routine 200 OK requests
- `/metrics` and `/health` requests don't generate app logs (skipped via `SKIP_TRACING`)

---

### 3. Metrics (Prometheus)

#### Custom Metrics
| Metric | Type | Labels | Purpose |
|--------|------|--------|---------|
| `app_requests_total` | Counter | `span_name`, `status` | Total request count per endpoint |
| `app_request_duration_seconds` | Histogram | `span_name` | Request latency distribution |

#### Exposition
- `/metrics` endpoint returns Prometheus text format via `prometheus_client`
- Prometheus scrapes at configured interval (default 15s)

---

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | Endpoints, middleware, auth, config |
| `tracing.py` | @traced decorator, log_span, filters, setup(), Prometheus metrics |
| `db.py` | Database queries with @traced spans |
| `logging_config.json` | dictConfig for console logging |
| `requirements.txt` | Dependencies |

---

## Current Limitations / Future Work

| Item | Status |
|------|--------|
| MongoDB auto-instrumentation | Not used — manual @traced on db.py |
| Span attributes on DB queries | Could add user_id, query type as attributes |
| Endpoint-level metrics | Counter uses span_name, could use http.route |
| Error count metric | No dedicated error counter — only status="error" label |
| Custom log shipper | Senior dev has a script that can replace python-logging-loki |
| Prometheus metrics panels | Not added to Grafana dashboard yet |

---

## Discussion Points

1. **Should we add custom attributes to DB spans?** (e.g., which user was searched, form_record_id)
2. **Should we instrument MongoDB with OTel instrumentor instead of manual @traced?**
3. **Do we need more granular metrics?** (per-endpoint latency, error rates)
4. **What's the right granularity for log suppression?** (current: only uvicorn.access at WARNING)
5. **Should span_completed logs go to Loki or stay console-only?**
