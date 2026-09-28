-- =============================================================================
-- Console: payload library, traffic jobs and schedulable system jobs.
--
-- console.payload_samples  JSON payloads per workflow (may contain {{placeholders}})
-- console.jobs             traffic generators: create / edit / run now / pause / resume
-- console.job_runs         one row per execution of a job
-- console.job_submissions  every request a job sent: HTTP status, response body, latency
-- console.system_jobs      maintenance jobs run by il-scheduler (reaper, partitions, DWH)
-- console.system_job_runs  execution history of system jobs
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS console;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'console_app') THEN
        CREATE ROLE console_app NOLOGIN;
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- Payload library
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS console.payload_samples (
    sample_id      integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workflow_name  text        NOT NULL REFERENCES integration.workflow_registry ON UPDATE CASCADE,
    name           text        NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    description    text,
    payload        jsonb       NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_name, name)
);

-- -----------------------------------------------------------------------------
-- Traffic jobs
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS console.jobs (
    job_id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name              text        NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    description       text,
    workflow_name     text        NOT NULL REFERENCES integration.workflow_registry ON UPDATE CASCADE,
    sample_id         integer     REFERENCES console.payload_samples ON DELETE SET NULL,
    payload_template  jsonb,                        -- used when set, otherwise the sample's payload
    interval_seconds  integer     CHECK (interval_seconds IS NULL OR interval_seconds BETWEEN 5 AND 86400),
    requests_per_run  integer     NOT NULL DEFAULT 1 CHECK (requests_per_run BETWEEN 1 AND 100),
    max_runs          integer     CHECK (max_runs IS NULL OR max_runs >= 1),
    use_callback      boolean     NOT NULL DEFAULT true,
    status            text        NOT NULL DEFAULT 'PAUSED' CHECK (status IN ('ACTIVE', 'PAUSED', 'COMPLETED')),
    run_requested     boolean     NOT NULL DEFAULT false,
    run_count         integer     NOT NULL DEFAULT 0,
    total_submitted   integer     NOT NULL DEFAULT 0,
    next_run_at       timestamptz,
    last_run_at       timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CHECK (payload_template IS NOT NULL OR sample_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS console.job_runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       integer     NOT NULL REFERENCES console.jobs ON DELETE CASCADE,
    trigger      text        NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    requested    integer     NOT NULL DEFAULT 0,
    accepted     integer     NOT NULL DEFAULT 0,       -- 2xx from the Integration Layer
    rejected     integer     NOT NULL DEFAULT 0,       -- 4xx (e.g. schema validation)
    errors       integer     NOT NULL DEFAULT 0,       -- 5xx or network errors
    error        text
);
CREATE INDEX IF NOT EXISTS ix_job_runs_job ON console.job_runs (job_id, run_id DESC);

CREATE TABLE IF NOT EXISTS console.job_submissions (
    submission_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id         bigint      NOT NULL REFERENCES console.job_runs ON DELETE CASCADE,
    job_id         integer     NOT NULL,
    seq            integer     NOT NULL,
    request_id     uuid,
    http_status    integer,
    payload        jsonb,
    response_body  jsonb,
    latency_ms     integer,
    submitted_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_job_submissions_run ON console.job_submissions (run_id);

-- -----------------------------------------------------------------------------
-- System (maintenance) jobs: schedule lives here, code lives in il-scheduler
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS console.system_jobs (
    job_name          text        PRIMARY KEY,
    description       text        NOT NULL,
    interval_seconds  integer     NOT NULL CHECK (interval_seconds BETWEEN 10 AND 604800),
    is_paused         boolean     NOT NULL DEFAULT false,
    run_requested     boolean     NOT NULL DEFAULT false,
    next_run_at       timestamptz NOT NULL DEFAULT now(),
    last_started_at   timestamptz,
    last_finished_at  timestamptz,
    last_status       text        CHECK (last_status IN ('OK', 'ERROR')),
    last_result       text,
    last_duration_ms  integer,
    run_count         integer     NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS console.system_job_runs (
    run_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_name     text        NOT NULL REFERENCES console.system_jobs ON DELETE CASCADE,
    trigger      text        NOT NULL CHECK (trigger IN ('schedule', 'manual')),
    started_at   timestamptz NOT NULL,
    duration_ms  integer,
    status       text        NOT NULL CHECK (status IN ('OK', 'ERROR')),
    result       text
);
CREATE INDEX IF NOT EXISTS ix_system_job_runs ON console.system_job_runs (job_name, run_id DESC);

-- updated_at maintenance
DROP TRIGGER IF EXISTS set_updated_at ON console.payload_samples;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON console.payload_samples
    FOR EACH ROW EXECUTE FUNCTION integration.trg_set_updated_at();
DROP TRIGGER IF EXISTS set_updated_at ON console.jobs;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON console.jobs
    FOR EACH ROW EXECUTE FUNCTION integration.trg_set_updated_at();

-- -----------------------------------------------------------------------------
-- Seed data
-- -----------------------------------------------------------------------------
INSERT INTO console.system_jobs (job_name, description, interval_seconds) VALUES
    ('reap_expired_leases',  'Re-queue requests whose App2 worker lease expired', 60),
    ('ensure_partitions',    'Create monthly request_metadata partitions ahead of time', 21600),
    ('dwh_load_incremental', 'Load changed requests into the data warehouse (analytics)', 60)
ON CONFLICT (job_name) DO NOTHING;

INSERT INTO console.payload_samples (workflow_name, name, description, payload) VALUES
    ('order_validation', 'Approved order', 'Small order within the credit limit',
     '{"order_id": "ORD-{{short_id}}", "customer_id": "CUST-1001", "currency": "EUR", "order_date": "{{date}}",
       "items": [{"sku": "SKU-100", "quantity": 2, "unit_price": 49.5}, {"sku": "SKU-200", "quantity": 1, "unit_price": 120}]}'),
    ('order_validation', 'Rejected: over credit limit', 'Bulk order that exceeds the 50,000 credit limit',
     '{"order_id": "ORD-{{short_id}}", "customer_id": "CUST-2002", "currency": "USD",
       "items": [{"sku": "SKU-900", "quantity": 1200, "unit_price": 75}]}'),
    ('order_validation', 'Invalid payload', 'Breaks the input schema: returns HTTP 422 with field errors',
     '{"order_id": "", "currency": "euro", "items": [{"sku": "SKU-1", "quantity": 0, "unit_price": -5}], "note": "extra field"}'),
    ('order_validation', 'Downstream outage', 'App2 raises a retryable error: retries with backoff, then FAILED',
     '{"order_id": "ORD-{{short_id}}", "customer_id": "CUST-DOWNSTREAM-DOWN", "currency": "EUR",
       "items": [{"sku": "SKU-100", "quantity": 1, "unit_price": 10}]}'),
    ('order_validation', 'Random order', 'Randomised order: placeholders are filled on every send',
     '{"order_id": "ORD-{{seq}}-{{short_id}}", "customer_id": "CUST-{{rand_int:1000:1099}}", "currency": "{{choice:EUR|USD|GBP}}",
       "order_date": "{{date}}", "items": [{"sku": "SKU-{{rand_int:100:999}}", "quantity": "{{rand_int:1:40}}",
       "unit_price": "{{rand_float:5:3000}}"}]}')
ON CONFLICT (workflow_name, name) DO NOTHING;

INSERT INTO console.jobs (name, description, workflow_name, sample_id, interval_seconds, requests_per_run, max_runs, status)
SELECT v.name, v.description, 'order_validation', s.sample_id, v.interval_seconds, v.per_run, v.max_runs, 'PAUSED'
FROM (VALUES
    ('Steady order traffic', 'Random orders every 20 s; about 1 in 5 exceeds the credit limit', 'Random order', 20, 3, 180),
    ('Credit-limit rejections', 'Business rejections (COMPLETED / REJECTED)', 'Rejected: over credit limit', NULL, 2, NULL),
    ('Downstream outage drill', 'Retryable failures that end in FAILED after retries', 'Downstream outage', NULL, 1, NULL),
    ('Invalid payload probe', 'Schema violations rejected with HTTP 422', 'Invalid payload', NULL, 1, NULL)
) AS v(name, description, sample, interval_seconds, per_run, max_runs)
JOIN console.payload_samples s ON s.workflow_name = 'order_validation' AND s.name = v.sample
ON CONFLICT (name) DO NOTHING;

-- -----------------------------------------------------------------------------
-- Privileges
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA console TO console_app, il_scheduler, ops_viewer;

-- Console UI: manages samples and jobs, can pause / trigger / reschedule system jobs
GRANT SELECT, INSERT, UPDATE, DELETE ON console.payload_samples, console.jobs TO console_app;
GRANT SELECT ON console.job_runs, console.job_submissions, console.system_jobs, console.system_job_runs TO console_app;
GRANT UPDATE (interval_seconds, is_paused, run_requested, next_run_at) ON console.system_jobs TO console_app;

-- Scheduler: executes both kinds of jobs
GRANT SELECT ON console.payload_samples TO il_scheduler;
GRANT SELECT, UPDATE ON console.jobs, console.system_jobs TO il_scheduler;
GRANT SELECT, INSERT, UPDATE ON console.job_runs TO il_scheduler;
GRANT INSERT ON console.job_submissions, console.system_job_runs TO il_scheduler;

-- Read-only viewers see everything in the console schema; telemetry needs session stats
GRANT SELECT ON ALL TABLES IN SCHEMA console TO ops_viewer;
GRANT pg_read_all_stats TO ops_viewer;
