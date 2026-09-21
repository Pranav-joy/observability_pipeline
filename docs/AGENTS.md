# Agent Instructions

## Exploration Policy
- Do NOT read the entire codebase or every file in a directory before making a change.
- Use grep/search tools to locate the specific function, class, or endpoint relevant to the task first.
- Only read files you have a concrete reason to read (the target file, its direct callers, its tests).
- If the user names specific files or describes the flow, trust that description and go straight to those files — do not re-verify by reading unrelated files "just in case."
- Cap exploration: if you can't find what you need in 2-3 targeted searches, ask the user rather than reading broadly.

## Project Structure
- Single-file FastAPI app (main.py, ~265 lines) — all endpoints, auth, logging, tracing middleware in one place
- tracing.py — OpenTelemetry setup, log_span(), @traced decorator, TraceContextFilter, ErrorExtractionFilter
- app/ directory is empty; code lives at repo root
- alloy/, loki/, grafana/, promtail/ are infrastructure configs
- Orchestrated via docker-compose.yml (5 services)

## Build / Test / Lint
- Run locally: uvicorn main:app --host 127.0.0.1 --port 8000 --reload
- Run via Docker: docker compose up
- Install deps: pip install -r requirements.txt
- No test framework, linter, or formatter configured

## Conventions
- Structured JSON logging via pythonjsonlogger
- OpenTelemetry tracing with manual spans via @traced decorator
- Parent span logged via log_span() in tracing_middleware (main.py)
- Span status: OK for 1xx-4xx, ERROR for 5xx and exceptions
- JWT auth via PyJWT, sets OTEL baggage per request
- 4-space indent, no type annotations, no docstrings
- No tests exist