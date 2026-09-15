# Changes

## Label Cleanup — Grafana Query Builder

**Goal:** Only `service_name` appears in the Grafana label filter dropdown during panel creation. Parsed attributes (level, user_id, etc.) moved to static/regex filters.

### Changes

| File | Line | Change |
|------|------|--------|
| `grafana/provisioning/dashboards/json/observability-api.json` | 89 | Remove `\| json` from Log Volume query: `{service_name="observability-api-1"}` |
| `grafana/provisioning/dashboards/json/observability-api.json` | 150 | Replace `\| json \| level=~"$level"` with `\|~ "level.*\"$level\""` |
| `grafana/provisioning/dashboards/json/observability-api.json` | 328 | Update template variable query to use regex instead of `\| json \| level` |
| `tracing.py` | 124 | Remove `"application": name` from `LokiHandler` tags |

### Before → After

**Log Volume query:**
- Before: `{service_name="observability-api-1"} | json`
- After: `{service_name="observability-api-1"}`

**Logs query:**
- Before: `{service_name="observability-api-1"} | json | level=~"$level"`
- After: `{service_name="observability-api-1"} |~ "level.*\"$level\""`

**Template variable query:**
- Before: `query_result({service_name="observability-api-1"} | json | level)`
- After: `query_result({service_name="observability-api-1"} |~ "\"level\"")`

**LokiHandler tags:**
- Before: `tags={"application": name, "service_name": "observability-api-1"}`
- After: `tags={"service_name": "observability-api-1"}`

### Tradeoff

- Label dropdown: 10+ fields → only `service_name`
- Filtering: structured `level=~` → regex `|~ "level.*..."`
- Slightly less precise regex, but clean query builder UI
