# Integration Layer

App1 → **Integration Layer (FastAPI)** → **PostgreSQL** ← **App2 (FastAPI worker)**, with results
returned to App1 by **polling** or **signed callback**, and a **data warehouse** schema for reporting.

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Plan and status: [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)

## Components

| Directory / service | What it does | Port |
|---|---|---|
| `postgres` | PostgreSQL 16: `integration` schema (queue + metadata) and `dwh` schema (star model) | 5432 |
| `migrate` | One-shot: applies `db/*.sql` in order and creates the service login users | – |
| `integration_layer/` → `il-api` | REST API for App1, JSON Schema validation, API call logging, callback delivery | 8000 |
| `integration_layer/` → `il-scheduler` | Lease reaper (1 min), monthly partitions (6 h), DWH incremental load (15 min) | – |
| `app2/` → `app2` | Claims requests, runs the workflow handler, validates output, reports the result | 8001 |
| `app1_mock/` → `app1-mock` | Stand-in for App1: submits orders and receives callbacks; `il_client.py` is a reusable client | 8002 |
| `ui/` → `ui` | **Operations console**: live dashboard, send test requests, request explorer (metadata, JSON, status history, API calls), callbacks, API log, workflows, DWH reports | 8080 (localhost only) |

## Run it

```bash
cp .env.example .env          # then change every password and secret
docker compose up -d --build
docker compose ps             # migrate should show "exited (0)", everything else healthy
python scripts/smoke_test.py  # end-to-end check (needs: pip install httpx)
```

**Console: http://localhost:8080** (tabs can be linked directly, e.g. `/#requests`, `/#send`).
It reads PostgreSQL with the read-only `ui_reader` user and performs actions (send, cancel, replay) through
the Integration Layer API server-side, so API keys never reach the browser. It holds the admin key, so it is
published on 127.0.0.1 only; put authentication in front of it before exposing it on a network.

API docs (Swagger UI): http://localhost:8000/docs, http://localhost:8001/docs, http://localhost:8002/docs

## Using the API (App1)

```bash
# Submit (202 Accepted). Resubmitting the same idempotency_key returns the original (200).
curl -s -X POST localhost:8000/v1/requests \
  -H 'X-API-Key: app1-dev-key' -H 'Content-Type: application/json' -d '{
    "workflow_name": "order_validation",
    "idempotency_key": "ORD-1001",
    "callback_url": "http://app1-mock:8002/hooks/il",
    "payload": {"order_id": "ORD-1001", "customer_id": "C-9", "currency": "EUR",
                "items": [{"sku": "A-1", "quantity": 2, "unit_price": 49.5}]}
  }'

# Poll
curl -s localhost:8000/v1/requests/<request_id> -H 'X-API-Key: app1-dev-key'
```

| Method | Path | Who |
|---|---|---|
| POST | `/v1/requests` | App |
| GET | `/v1/requests/{id}` (`?include_input=true`) | App (own requests only) |
| GET | `/v1/requests?status=&workflow_name=&from_date=&to_date=&limit=&offset=` | App |
| POST | `/v1/requests/{id}/cancel` | App (PENDING only) |
| GET | `/v1/workflows`, `/v1/workflows/{name}` | App (view the JSON Schemas) |
| PUT | `/v1/admin/workflows/{name}` | Admin (register/update; schema changes bump `schema_version`) |
| POST | `/v1/admin/requests/{id}/replay` | Admin (FAILED only) |
| GET | `/health`, `/health/ready` | – |

**Error format:** `{"error": {"code": "...", "message": "...", "errors": [{"path": "$.items[0].quantity", "message": "...", "rule": "minimum"}]}}`.
The codes are `UNAUTHORIZED`, `REQUEST_INVALID`, `UNKNOWN_WORKFLOW`, `INPUT_SCHEMA_INVALID`,
`CALLBACK_HOST_NOT_ALLOWED`, `PAYLOAD_TOO_LARGE`, `NOT_FOUND`, `NOT_CANCELLABLE`,
`NOT_REPLAYABLE` and `INVALID_JSON_SCHEMA`.

### Callbacks

When a request reaches COMPLETED, FAILED or CANCELLED and has a `callback_url`, the IL POSTs:

```json
{"event": "request.completed", "request_id": "...", "correlation_id": "...", "workflow_name": "order_validation",
 "status": "COMPLETED", "success_flag": true, "workflow_status": "APPROVED",
 "output_json": {...}, "error": null, "received_at": "...", "completed_at": "...", "attempt": 1}
```

Headers: `X-IL-Request-Id`, `X-Correlation-Id`, `X-IL-Attempt`, `X-IL-Timestamp`, and
`X-IL-Signature: sha256=HMAC(secret, "<timestamp>.<body>")`. Verify the signature with
`app1_mock.il_client.verify_signature`. A non-2xx response is retried with backoff
(10 s, 20 s, 40 s, …, up to 30 min), 6 attempts by default. Polling works whether or not a callback is set.

## Adding a workflow

1. Register its schemas:
   `PUT /v1/admin/workflows/<name>` with `{"target_app": "app2", "input_schema": {...}, "output_schema": {...}, "max_retries": 3}`
2. Add a handler in [app2/processors.py](app2/processors.py):

   ```python
   @handler("<name>")
   async def my_workflow(payload: dict) -> Result:
       ...                                   # raise RetryableError / PermanentError on failure
       return Result(output={...}, workflow_status="DONE")
   ```

Requests for a workflow that App2 has no handler for fail with `NO_HANDLER`. Output that does not
match `output_schema` fails with `OUTPUT_SCHEMA_INVALID`.

## Database access

| Login user | Role | Can do |
|---|---|---|
| `il_api` | `il_service` | submit/cancel/replay functions, callback functions, read status view, write `api_call_log` and `workflow_registry` |
| `app2` | `app2_worker` | **only** `claim_requests`, `heartbeat`, `complete_request`, `fail_request` + read output schemas |
| `il_scheduler_user` | `il_scheduler`, `dwh_etl` | reaper, partition creation, DWH load |
| `bi_reader` | `dwh_reader` | read-only on the `dwh` schema (connect your BI tool with this) |
| `ui_reader` | `ops_viewer` | read-only on the `integration` and `dwh` schemas (operations console) |

Useful queries:

```sql
-- Queue health
SELECT status, count(*), min(received_at) AS oldest FROM integration.request_metadata GROUP BY status;
-- Full history of one request
SELECT * FROM integration.request_event_log WHERE request_id = '<id>' ORDER BY event_id;
SELECT * FROM integration.api_call_log      WHERE request_id = '<id>' ORDER BY call_id;
-- Daily report
SELECT * FROM dwh.v_daily_workflow_summary ORDER BY full_date DESC;
```

## Tests

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
TEST_PG_ADMIN_URL=postgresql://postgres:<POSTGRES_PASSWORD>@localhost:5432/postgres pytest
```

The tests create and drop their own database and `test_*` login users; the service users and the
`integration` database are not touched. Still, run them against a development server, not production.
