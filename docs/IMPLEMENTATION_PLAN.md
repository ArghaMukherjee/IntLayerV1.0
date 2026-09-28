# Integration Layer: Implementation Plan and Status

Companion to [ARCHITECTURE.md](ARCHITECTURE.md). Last updated 2026-09-28.

## Confirmed decisions

| Topic | Decision | Effect on the build |
|---|---|---|
| Result delivery | Polling **and** callback | `GET /v1/requests/{id}` + signed, retried webhook tracked in `request_metadata.callback_*` |
| Stack | Python 3.12 + FastAPI for IL and App2 | Shared libraries: psycopg 3 (async pool), pydantic-settings, jsonschema |
| Volume | 500–1,000 requests/day | 1 replica per service; no PgBouncer or broker needed; monthly partitions kept |
| Hosting | Docker containers | `docker-compose.yml`; a scheduler container replaces `pg_cron` |
| Validation | JSON Schema required | `workflow_registry` with input/output schemas; IL validates input, App2 validates output |

## Status

| Phase | Scope | Status |
|---|---|---|
| 1 | Database: schema, state-machine functions, roles, DWH | ✅ Done, tested |
| 1b | Scheduled jobs (reaper, partitions, DWH load) | ✅ Done: `integration_layer/scheduler.py` |
| 2 | Integration Layer API, validation, API logging, callbacks | ✅ Done, tested |
| 3 | App2 worker (FastAPI) | ✅ Done, tested |
| 4 | Docker Compose, App1 mock, smoke test | ✅ Built; see "Verification" for what was run |
| 5 | Data warehouse reporting / BI | 🟡 Schema + load + summary view done; BI tool hookup pending |
| 6 | Hardening and operations | ⬜ Not started |

## Repository layout

```
IntegrationLayer/
├── db/
│   ├── 001_integration_schema.sql   tables, triggers, functions, roles, grants
│   ├── 002_dwh_schema.sql           star schema, load procedure, summary view
│   └── 003_seed_workflows.sql       example workflow: order_validation
├── integration_layer/               IL service (image: integration-layer)
│   ├── main.py                      app factory, error format, lifespan
│   ├── routes/                      requests.py, workflows.py, health.py
│   ├── validation.py                JSON Schema catalog (cached)
│   ├── api_log.py                   api_call_log middleware (async writes, masked secrets)
│   ├── callbacks.py                 callback dispatcher (LISTEN + poll, HMAC, retries)
│   ├── scheduler.py                 maintenance jobs (separate container)
│   └── migrate.py                   migration runner + login users (one-shot container)
├── app2/                            App2 service
│   ├── processors.py                workflow handlers ← business logic goes here
│   ├── worker.py                    claim / heartbeat / complete / fail loop
│   └── queue_client.py              the 4 SQL calls App2 is allowed to make
├── app1_mock/                       App1 stand-in + reusable il_client.py
├── tests/                           pytest (29 tests, real PostgreSQL)
├── scripts/smoke_test.py            end-to-end check against a running stack
├── docker-compose.yml, .env.example, requirements-dev.txt
```

## Verification performed

On PostgreSQL 16.4 (a local portable build) with the three services running under uvicorn:

- **pytest: 29 passed.** The suite covers exclusive claims with 4 concurrent workers, retries and
  backoff, permanent failure, lease reaping, the lease-holder check, idempotency, the
  audit trail, callback queue/backoff/give-up, least-privilege roles, DWH idempotency,
  API auth and ownership, schema errors by JSON path, schema versioning, the callback
  host allow-list, API log masking, and all six App2 worker outcomes.
- **Smoke test: 21/21 passed.** It covers the full App1 → IL → Postgres → App2 → IL → signed
  callback to App1 path, the polling path, a business rejection, and a retryable failure.
- The migrator is idempotent (re-run applies 0), the scheduler ran all 3 jobs, and the DWH summary
  was readable by `bi_reader`, who is denied the OLTP tables.
- **Not yet run:** `docker compose up`. The Docker daemon was not running on the build machine.
  The Compose file passes `docker compose config`, and every pinned dependency was confirmed
  to have Python 3.12 Linux wheels (x86_64 and arm64).

## Remaining work

### Phase 4 wrap-up
- [ ] `docker compose up -d --build` then `python scripts/smoke_test.py` on the target host
- [ ] Replace `app1_mock` with the real App1 (use `app1_mock/il_client.py` or plain HTTP)
- [ ] Move the real business logic into `app2/processors.py` and register the real workflows'
      JSON Schemas via `PUT /v1/admin/workflows/{name}`

### Phase 5: Reporting
- [ ] Connect a BI tool (Metabase fits well as another container) with the `bi_reader` user
- [ ] Extra marts if needed: hourly API traffic from `api_call_log`, error-code breakdown

### Phase 6: Hardening
- [ ] TLS in front of `il-api` (reverse proxy such as Traefik, Caddy or nginx); keep 8001/8002 internal
- [ ] Secrets from a vault or Docker secrets instead of `.env`
- [ ] Backups: nightly `pg_dump` container + WAL archiving if point-in-time restore is needed
- [ ] Retention job: detach/archive `request_metadata` partitions older than 90 days
- [ ] Metrics (`/metrics`, Prometheus) and alerts: oldest PENDING > 5 min, FAILED rate,
      callback FAILED, reaper recoveries > 0
- [ ] Structured JSON logs with `correlation_id`
- [ ] CI pipeline: pytest against a `postgres:16` service container, image build, smoke test
