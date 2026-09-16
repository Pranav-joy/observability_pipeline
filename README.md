# Observability Stack

Non-invasive structured logging and trace correlation for FastAPI services.

---

## What You Get

| Pillar | What |
|--------|------|
| **Logs** | Structured JSON in Loki with `trace_id`, `user_id`, `org_id` auto-injected into every log line |
| **Correlation** | Every log line links to its trace. Filter by user across all logs. |

## Quick Start

```bash
# 1. Add dependencies
pip install opentelemetry-api opentelemetry-sdk opentelemetry-instrumentation-fastapi \
            opentelemetry-instrumentation-httpx opentelemetry-exporter-otlp-proto-grpc \
            opentelemetry-processor-baggage python-json-logger python-logging-loki

# 2. Set environment variables
export LOKI_URL=http://your-loki:3100
export SERVICE_NAME=your-service-name

# 3. Add to your FastAPI app
```

```python
import json
from fastapi import FastAPI
from tracing import setup, instrument_app

app = FastAPI()
instrument_app(app)
logger = setup()
```

That's it. Every request now gets a trace ID. Every log line carries it.

## Files

| File | What It Does |
|------|-------------|
| `tracing.py` | Observability library — copy this into your project |
| `logging_config.json` | Structured JSON logging config for console output |
| `SETUP.md` | Detailed integration guide |
| `FEATURE.md` | What we built, why, and how it works |

## Architecture

```
Request → FastAPI Auto-Instrumentor → Your Endpoints → Tracing Middleware
                                                            │
                                                            ▼
                                                         Loki
                                                   (structured JSON
                                                      logs with
                                                   trace_id, user_id)
                                                            │
                                                            ▼
                                                        Grafana
                                                       (dashboards)
```

## How It Works

1. Request arrives → span created with unique trace ID
2. Auth sets `user_id`/`org_id` via OpenTelemetry baggage
3. Every `logger.info()` gets `trace_id`, `user_id`, `org_id` injected automatically
4. Structured JSON pushed to Loki — queryable by any field
5. Middleware logs span completion with duration and status

## Grafana Dashboards

Import the dashboard JSONs from `grafana/provisioning/dashboards/`:

- **Log viewer** — filter by trace_id, user_id, endpoint, log level
- **Log volume** — request volume over time

Update the datasource UIDs and `service_name` in the queries to match your environment.

## Requirements

- Python 3.10+
- FastAPI
- Loki (for logs)
- Grafana (for dashboards)

Optional:
- Tempo (for distributed trace visualization)

## Documentation

- **[SETUP.md](SETUP.md)** — step-by-step integration guide with code snippets, config reference, and troubleshooting
- **[FEATURE.md](FEATURE.md)** — architecture, design decisions, and how it all works
