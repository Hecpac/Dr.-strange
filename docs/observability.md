# Observability

Claw keeps the local SQLite observe stream as the compatibility store while v3
migrates toward OpenTelemetry-backed traces.

## OpenTelemetry Export

The exporter is disabled by default. Enable it with either:

```bash
CLAW_OTEL_ENABLED=true
```

or by setting an OTLP endpoint:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces
```

Optional service name:

```bash
OTEL_SERVICE_NAME=claw-core
```

When the OpenTelemetry SDK/exporter packages are unavailable, Claw fails open:
events continue to write to SQLite and `ObserveStream.telemetry_status()` reports
the exporter as unavailable.

Payloads are redacted before export for keys containing `api_key`, `token`,
`secret`, `password`, or `authorization`.

## Local Control API

The local API keeps trace endpoints during the migration for backwards
compatibility. Durable control endpoints now use the v3 services:

- `GET /api/jobs`
- `GET /api/jobs/<job_id>`
- `DELETE /api/jobs/<job_id>`
- `GET /api/approvals`
- `GET /api/approvals/<approval_id>`
- `POST /api/approvals/<approval_id>/approve`
- `POST /api/approvals/<approval_id>/reject`
