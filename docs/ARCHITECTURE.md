# Integration Layer: Architecture

## 1. Goal

App1 and App2 never talk to each other directly. They exchange data through an
**Integration Layer (IL)** backed by **PostgreSQL**, which acts as the shared
message store, audit record and the source for a **data warehouse (DWH)**.

- **App1** (producer) sends a JSON request to the IL over HTTP.
- **IL** saves the request, with full metadata, in Postgres.
- **App2** (consumer) picks up pending requests from Postgres, processes them and
  writes the output JSON and the final status back to Postgres.
- **IL** detects the completion and returns the response to App1 (App1 either
  polls or receives a callback).
- **DWH layer** loads the operational data on a schedule into a star schema for
  reporting and analytics.

### Confirmed decisions

| Topic | Decision |
|---|---|
| Returning results to App1 | **Both** polling (`GET /v1/requests/{id}`) and signed webhook callbacks |
| Stack | **Python 3.12 + FastAPI** for the Integration Layer and for App2 |
| Volume | **500–1,000 requests/day** (≈1/min on average), so a single Postgres instance has plenty of headroom |
| Hosting | **Docker containers** (Docker Compose); scheduled jobs run in a Python scheduler container, not `pg_cron` |
| Validation | **JSON Schema** per workflow: the IL validates App1's input, and App2 validates its own output |

## 2. High-level view

```
                        ┌──────────────────────────────────────────────┐
                        │              INTEGRATION LAYER (IL)          │
 ┌────────┐  POST /v1/  │  ┌────────────┐   ┌───────────────────────┐  │
 │        │  requests   │  │  REST API  │   │  Completion Listener  │  │
 │  APP1  │────────────▶│  │ (ingress)  │   │ (LISTEN request_done) │  │
 │        │◀────────────│  └─────┬──────┘   └──────────┬────────────┘  │
 │        │ 202 + id    │        │                     │               │
 │        │             │        │ INSERT              │ callback POST │
 │        │ GET /v1/    │        ▼                     │ to App1       │
 │        │ requests/id │  ┌──────────────────────┐    │ (optional)    │
 │        │────────────▶│  │  Postgres pool (IL   │    │               │
 │        │◀────────────│  │  role: il_service)   │    │               │
 └────────┘ status+out  │  └─────────┬────────────┘    │               │
      ▲                 └────────────┼─────────────────┼───────────────┘
      │ callback                     │                 │
      └──────────────────────────────┼─────────────────┘
                                     ▼
            ┌───────────────────────────────────────────────────────────┐
            │                        POSTGRESQL                         │
            │                                                           │
            │  schema: integration (OLTP)                               │
            │   ├─ workflow_registry  (workflows + JSON Schemas)        │
            │   ├─ request_metadata   (1 row per request, partitioned)  │
            │   ├─ api_call_log       (every GET/POST hitting the IL)   │
            │   ├─ request_event_log  (state-transition audit trail)    │
            │   └─ functions: submit / claim / complete / fail / reap / │
            │                 claim_callbacks / record_callback_result  │
            │                                                           │
            │  NOTIFY 'request_new'  ─────────┐                         │
            │  NOTIFY 'request_done' ──▶ IL   │                         │
            │                                 │                         │
            │  schema: dwh (star schema)      │   ◀── scheduled ETL     │
            │   ├─ dim_date, dim_application, dim_workflow,             │
            │   ├─ dim_server, dim_status                               │
            │   └─ fact_request                                         │
            └─────────────────────────────────┼─────────────────────────┘
                                              │ LISTEN request_new
                                              │ + polling (fallback)
                                              ▼
                               ┌──────────────────────────────┐
                               │            APP2              │
                               │  worker(s), role: app2_worker│
                               │  1. claim (SKIP LOCKED)      │
                               │  2. process                  │
                               │  3. complete / fail          │
                               └──────────────────────────────┘
```

**Access rule:** App1 never connects to Postgres; it talks only to the IL API.
App2 connects to Postgres with a restricted role that can **only execute the
queue functions** (`claim_requests`, `complete_request`, `fail_request`,
`heartbeat`). It has no direct table access, so it cannot corrupt metadata.

## 3. End-to-end flow (sequence)

```
App1              IL API                 Postgres                    App2 worker        IL Listener
 │  POST /v1/requests │                        │                          │                  │
 │  {payload,         │                        │                          │                  │
 │   idempotency_key} │                        │                          │                  │
 │───────────────────▶│ validate JSON schema   │                          │                  │
 │                    │ capture server/client  │                          │                  │
 │                    │ details                │                          │                  │
 │                    │ submit_request(...) ──▶│ INSERT request_metadata  │                  │
 │                    │                        │   status=PENDING         │                  │
 │                    │                        │ INSERT api_call_log      │                  │
 │                    │                        │ INSERT request_event_log │                  │
 │                    │                        │ NOTIFY request_new ─────▶│                  │
 │◀───────────────────│ 202 {request_id}       │                          │                  │
 │                    │                        │◀─ claim_requests(n) ─────│                  │
 │                    │                        │   FOR UPDATE SKIP LOCKED │                  │
 │                    │                        │   status=IN_PROGRESS     │                  │
 │                    │                        │   locked_until=now()+5m  │                  │
 │                    │                        │── rows ─────────────────▶│ process…         │
 │                    │                        │◀─ heartbeat(id) ─────────│ (long jobs)      │
 │                    │                        │◀─ complete_request(id,   │                  │
 │                    │                        │     output_json) ────────│                  │
 │                    │                        │   status=COMPLETED       │                  │
 │                    │                        │   success_flag=true      │                  │
 │                    │                        │   workflow_completed_at  │                  │
 │                    │                        │ NOTIFY request_done ─────┼─────────────────▶│
 │                    │                        │                          │   callback App1  │
 │◀───────────────────┼────────────────────────┼──────────────────────────┼──────────────────│
 │  (or) GET /v1/requests/{id}                 │                          │                  │
 │───────────────────▶│ SELECT … ─────────────▶│                          │                  │
 │◀───────────────────│ 200 {status, output_json}                         │                  │
```

## 4. Request lifecycle (state machine)

```
            submit                claim                 complete
 (none) ──────────▶ PENDING ─────────────▶ IN_PROGRESS ──────────▶ COMPLETED   (success_flag = true)
                      ▲                       │    │
                      │ fail(retryable) and   │    │ fail(non-retryable) or
                      │ retry_count < max     │    │ retry_count >= max_retries
                      │ (next_attempt_at =    │    ▼
                      │  now + backoff)       │  FAILED                        (success_flag = false)
                      └───────────────────────┘
                      ▲                       │
                      │ reaper: lease expired │
                      └───────────────────────┘  (locked_until < now, App2 crashed)

 Any PENDING request can be moved to CANCELLED through the IL API.
```

| Status        | Meaning                                                           | success_flag |
|---------------|-------------------------------------------------------------------|--------------|
| `PENDING`     | Stored and waiting for App2 (new, or scheduled for retry)          | NULL         |
| `IN_PROGRESS` | Claimed by an App2 worker, lease active                            | NULL         |
| `COMPLETED`   | App2 finished the workflow and output_json is stored               | TRUE         |
| `FAILED`      | Permanent failure, or retries used up (acts as the dead-letter state) | FALSE     |
| `CANCELLED`   | Cancelled by App1 or an operator before processing                 | FALSE        |

## 5. Metadata captured (how requirements map to columns)

| Requirement                   | Where it is stored                                                                  |
|-------------------------------|-------------------------------------------------------------------------------------|
| Input JSON                    | `request_metadata.input_json` (JSONB)                                               |
| Date                          | `request_metadata.request_date` (DATE, also the partition key)                      |
| Timestamp                     | `received_at`, `picked_at`, `workflow_completed_at`, `updated_at` (TIMESTAMPTZ)     |
| API URL, GET and POST request | `request_metadata.api_url` / `http_method` (the submitting call) + **`api_call_log`**, one row for every GET/POST (URL, method, headers, status code, latency) |
| Server-level details          | `il_server_hostname`, `il_server_ip`, `il_instance_id`, `client_ip`, `app2_worker_host`, `app2_worker_id`, `db_server` (captured on insert) |
| Output JSON                   | `request_metadata.output_json` (JSONB)                                              |
| Workflow completion from App2 | `workflow_status`, `workflow_completed_at`, `processing_duration_ms`                 |
| Success/failure flag          | `status` (enum) + `success_flag` (boolean) + `error_code` / `error_message`          |

Other supporting columns: `request_id` (UUID primary key), `correlation_id` (end-to-end
tracing), `idempotency_key` (no duplicates when App1 resubmits), `source_app` /
`target_app`, `workflow_name`, `retry_count` / `max_retries` / `next_attempt_at`,
`locked_by` / `locked_until` (the lease), and `callback_url`.

Full DDL: [`db/001_integration_schema.sql`](../db/001_integration_schema.sql).

### JSON validation

`integration.workflow_registry` holds, per `workflow_name`: `target_app`, `input_schema`,
`output_schema` (JSON Schema draft 2020-12), `max_retries` and `schema_version`.

1. **IL (on submit):** the payload is checked against `input_schema`. Failures return
   **422 `INPUT_SCHEMA_INVALID`** with one entry per problem (`$.items[0].quantity: 0 is
   less than the minimum of 1`), and nothing is stored in `request_metadata` (the
   rejected call is still recorded in `api_call_log`). The version validated against is stored in
   `input_schema_version`.
2. **App2 (before completing):** `output_json` is checked against `output_schema`. A
   mismatch marks the request **FAILED / `OUTPUT_SCHEMA_INVALID`**, so a malformed
   response never reaches App1.
3. Schemas are managed through `PUT /v1/admin/workflows/{name}`. A schema change bumps
   `schema_version`, and the IL's cache refreshes within 30 s.

### Callback delivery

When a request reaches a terminal state and has a `callback_url`, a trigger sets
`callback_status = PENDING`. The IL dispatcher wakes on `NOTIFY request_done` and also polls
every 5 s. It claims due callbacks with `SKIP LOCKED`, so replicas never double-send, POSTs the result
signed with HMAC-SHA256 (`X-IL-Signature`), and records the outcome: `DELIVERED`, or a retry with
backoff of 10 s, 20 s, 40 s, … up to 30 min, then `FAILED` after 6 attempts. Every attempt is an
`OUTBOUND` row in `api_call_log`. `callback_url` hosts can be restricted with an allow-list.

## 6. Key design decisions

| Decision | Choice | Why |
|---|---|---|
| Queue mechanism | Postgres table + `SELECT … FOR UPDATE SKIP LOCKED` | No extra broker to run. Multiple App2 workers are safe and never claim the same row twice. |
| Low-latency wake-up | `LISTEN/NOTIFY` (`request_new`, `request_done`), with polling as a fallback | Workers react within milliseconds. Polling covers notifications lost during reconnects. |
| Crash safety | Lease (`locked_until`) + heartbeat + reaper job | If an App2 worker dies mid-job, the request returns to `PENDING` instead of being stuck. |
| Duplicate protection | Unique `(source_app, idempotency_key)` | A retried POST from App1 returns the existing `request_id`. |
| Payload storage | `JSONB` + GIN index on `input_json` | Flexible schema that can still be queried. |
| Volume management | `request_metadata` range-partitioned by month on `request_date` | Fast pruning. Old partitions can be detached or archived cheaply. |
| Encapsulation | All state changes go through SQL functions | One place enforces the state machine. App2 gets EXECUTE only. |
| Audit | `request_event_log` written by a trigger on every status change | Complete history for troubleshooting and for DWH SLA metrics. |
| Response to App1 | Polling `GET /v1/requests/{id}` (always available) + signed webhook callback | App1 can pick whichever suits it; callbacks are retried and tracked in the DB. |
| Scheduled jobs | `il-scheduler` container (Python) with advisory locks | Works on any Postgres (no `pg_cron` needed), safe if more than one runs. |
| DWH | Separate `dwh` schema, star model, incremental ETL on an `updated_at` watermark | Reporting does not load the OLTP tables. Can later move to a separate server or read replica. |

## 7. Integration Layer API (contract)

| Method | Path                                | Purpose                                            |
|--------|-------------------------------------|----------------------------------------------------|
| POST   | `/v1/requests`                      | Submit input JSON → `202 {request_id, status}`      |
| GET    | `/v1/requests/{id}`                 | Status + output_json once complete (polling)        |
| GET    | `/v1/requests?status=&from_date=`   | Search own requests                                 |
| POST   | `/v1/requests/{id}/cancel`          | Cancel while still PENDING                          |
| GET    | `/v1/workflows[/{name}]`            | Registered workflows and their JSON Schemas         |
| PUT    | `/v1/admin/workflows/{name}`        | Register / update a workflow (admin)                |
| POST   | `/v1/admin/requests/{id}/replay`    | Re-queue a FAILED request (admin)                   |
| GET    | `/health`, `/health/ready`          | Liveness / readiness                                |

Submit body:
```json
{
  "workflow_name": "order_validation",
  "idempotency_key": "app1-ord-100045",
  "callback_url": "https://app1.internal/hooks/il",
  "payload": { "...": "business JSON" }
}
```

## 8. Data warehouse layer

```
 integration.request_metadata ──┐
 integration.api_call_log      ─┼─▶ ETL proc dwh.load_incremental()  (il-scheduler, every 15 min)
 integration.request_event_log ─┘        │  watermark = dwh.etl_watermark.last_loaded_at
                                          ▼
          dim_date ─┐
   dim_application ─┤
      dim_workflow ─┼──▶  fact_request  (grain: one row per request, upserted until final state)
        dim_server ─┤       measures: queue_wait_ms, processing_ms, end_to_end_ms,
        dim_status ─┘                 retry_count, success_flag, input/output bytes
```

Example questions the DWH answers: daily volume per workflow, success rate,
p95 end-to-end latency, retry hotspots, and load by IL/App2 server.

DDL: [`db/002_dwh_schema.sql`](../db/002_dwh_schema.sql).

## 9. Non-functional concerns

- **Security:** TLS on the IL API; App1 authenticates with an API key or OAuth2
  client credentials. Separate DB roles (`il_service`, `app2_worker`,
  `dwh_etl`, `dwh_reader`). Sensitive headers (Authorization, cookies) are
  masked before they are written to `api_call_log`.
- **Observability:** `correlation_id` travels through IL → DB → App2 logs.
  Planned (Phase 6) Prometheus metrics: queue depth, age of the oldest PENDING request, failure rate.
- **Retention:** OLTP keeps 90 days (monthly partitions are detached and
  archived); the DWH keeps history long-term.
- **Scaling:** at 500–1,000 requests/day, one IL container and one App2
  container are enough, with large headroom (the design handles hundreds of
  requests per *second*). The IL is stateless and App2 uses SKIP LOCKED, so both
  scale by adding replicas if volume grows.
- **When to outgrow this:** above roughly 1–2k msgs/sec sustained, or when
  fan-out to many consumers is needed, keep Postgres as the system of record and
  add Kafka/RabbitMQ using the outbox pattern.

## 10. Deployment (Docker Compose)

```
 ┌───────────── docker network ──────────────────────────────────────────┐
 │  postgres:16 ◀── migrate (one-shot: db/*.sql + login users)           │
 │      ▲  ▲  ▲                                                          │
 │      │  │  └── il-scheduler  reaper 1 min · partitions 6 h · DWH 15 min│
 │      │  └───── app2  :8001   worker + /health /stats                  │
 │      └──────── il-api :8000  REST API + callback dispatcher           │
 │                  ▲      │ callback                                    │
 │                  │      ▼                                             │
 │               app1-mock :8002  (replace with the real App1)          │
 └───────────────────────────────────────────────────────────────────────┘
```
