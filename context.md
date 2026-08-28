I want to add OpenTelemetry tracing and log correlation to this Python prototype.

Current logging already works and outputs JSON like:

{
  "timestamp": "...",
  "level": "INFO",
  "name": "app",
  "message": "function_c completed",
  "request_id": "..."
}

Do NOT redesign or replace the existing JSON log structure.

Goal:
1. Add OpenTelemetry tracing to the application.
2. When an API request arrives:
   - If an existing trace context is present, continue/propagate it.
   - If no trace context exists, create a new trace.
3. Create/propagate spans for the relevant API/function operations.
4. Automatically make the current trace_id and span_id available to Python logging.
5. Enrich every application log with:
   - trace_id
   - span_id
6. Do NOT manually pass trace_id/span_id as function arguments.
7. Keep the existing fields such as timestamp, level, name, message, request_id, and elapsed_s.
8. The final JSON should look conceptually like:

{
  "timestamp": "...",
  "level": "INFO",
  "name": "app",
  "message": "function_c completed",
  "request_id": "...",
  "trace_id": "...",
  "span_id": "..."
}

The prototype currently has no real user concept, so DO NOT add user_id yet.

The observability stack is:
Python application
→ Promtail
→ Loki
→ Grafana

Tempo/OpenTelemetry tracing will be used for traces.

Before modifying code, inspect the existing project structure and logging implementation. Prefer the standard OpenTelemetry Python approach and make the smallest changes necessary.

The important requirement is:
OpenTelemetry should own trace/span creation and propagation; logging should automatically read the current OpenTelemetry context and attach trace_id/span_id to each JSON log.