"""Job runner (runs as its own container).

Two kinds of jobs, both scheduled from the database so the console can create,
trigger, pause, resume and reschedule them:

* System jobs (console.system_jobs): maintenance SQL (lease reaper, partitions,
  DWH load). The schedule is in the table; the SQL to run is defined here.
* Traffic jobs (console.jobs): send requests to the Integration Layer API built
  from a JSON payload template, recording every HTTP status and response body.

Due jobs are claimed with FOR UPDATE SKIP LOCKED, so running more than one
scheduler never runs a job twice.
"""
import logging
import signal
import threading
import time

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic_settings import BaseSettings, SettingsConfigDict

from .templating import render

log = logging.getLogger("scheduler")

SYSTEM_JOB_SQL = {
    "reap_expired_leases": "SELECT integration.reap_expired_leases()::text AS result",
    "ensure_partitions": "SELECT string_agg(integration.ensure_month_partition("
                         "(current_date + make_interval(months => m))::date), ', ') AS result "
                         "FROM generate_series(0, 2) AS m",
    "dwh_load_incremental": "CALL dwh.load_incremental()",
}


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", extra="ignore")

    database_url: str = "postgresql://il_scheduler_user:il_scheduler@localhost:5432/integration"
    tick_seconds: float = 2
    # Traffic jobs call the Integration Layer API as this application
    il_base_url: str = "http://localhost:8000"
    il_api_key: str = ""
    callback_url: str = "http://localhost:8002/hooks/il"
    http_timeout_seconds: float = 10


# ----------------------------------------------------------------------------- system jobs
def run_system_jobs(conn: psycopg.Connection) -> int:
    with conn.transaction():
        due = conn.execute(
            """SELECT job_name, interval_seconds, run_requested FROM console.system_jobs
               WHERE run_requested OR (NOT is_paused AND next_run_at <= now())
               FOR UPDATE SKIP LOCKED""").fetchall()
        for job in due:
            name = job["job_name"]
            trigger = "manual" if job["run_requested"] else "schedule"
            started, t0 = time.time(), time.perf_counter()
            status, result = "OK", None
            sql = SYSTEM_JOB_SQL.get(name)
            try:
                if sql is None:
                    raise ValueError(f"no implementation for system job '{name}'")
                with conn.transaction():  # savepoint: a failing job doesn't abort the others
                    cur = conn.execute(sql)
                    row = cur.fetchone() if cur.description else None
                    result = str(row["result"]) if row and row["result"] is not None else "done"
            except Exception as exc:
                status, result = "ERROR", f"{type(exc).__name__}: {exc}"[:1000]
            duration = int((time.perf_counter() - t0) * 1000)
            conn.execute(
                """UPDATE console.system_jobs
                      SET last_started_at = to_timestamp(%(started)s), last_finished_at = clock_timestamp(),
                          last_status = %(status)s, last_result = %(result)s, last_duration_ms = %(duration)s,
                          run_count = run_count + 1, run_requested = false,
                          next_run_at = clock_timestamp() + make_interval(secs => interval_seconds)
                    WHERE job_name = %(name)s""",
                {"started": started, "status": status, "result": result, "duration": duration, "name": name})
            conn.execute(
                """INSERT INTO console.system_job_runs (job_name, trigger, started_at, duration_ms, status, result)
                   VALUES (%s, %s, to_timestamp(%s), %s, %s, %s)""",
                (name, trigger, started, duration, status, result))
            log.log(logging.INFO if status == "OK" else logging.ERROR,
                    "system job=%s trigger=%s status=%s result=%s duration_ms=%d", name, trigger, status, result, duration)
    return len(due)


# ----------------------------------------------------------------------------- traffic jobs
def submit(http: httpx.Client, settings: SchedulerSettings, job: dict, payload) -> tuple[int | None, object, int]:
    body = {"workflow_name": job["workflow_name"], "payload": payload}
    if job["use_callback"]:
        body["callback_url"] = settings.callback_url
    t0 = time.perf_counter()
    try:
        response = http.post(f"{settings.il_base_url}/v1/requests", json=body,
                             headers={"X-API-Key": settings.il_api_key})
        try:
            data = response.json()
        except ValueError:
            data = {"text": response.text[:2000]}
        return response.status_code, data, int((time.perf_counter() - t0) * 1000)
    except httpx.HTTPError as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}, int((time.perf_counter() - t0) * 1000)


def run_traffic_job(conn: psycopg.Connection, http: httpx.Client, settings: SchedulerSettings, job: dict) -> None:
    trigger = "manual" if job["run_requested"] else "schedule"
    run_id = conn.execute(
        """INSERT INTO console.job_runs (job_id, trigger, requested, started_at)
           VALUES (%s, %s, %s, clock_timestamp()) RETURNING run_id""",
        (job["job_id"], trigger, job["requests_per_run"])).fetchone()["run_id"]
    template = job["payload_template"] if job["payload_template"] is not None else job["sample_payload"]
    counts = {"accepted": 0, "rejected": 0, "errors": 0}
    run_error = None
    if template is None:
        run_error = "Job has no payload: its sample was deleted and no template is set"
        counts["errors"] = job["requests_per_run"]
    else:
        for i in range(job["requests_per_run"]):
            seq = job["total_submitted"] + i + 1
            payload = render(template, {"seq": seq, "job": job["name"]})
            status, data, latency = submit(http, settings, job, payload)
            bucket = "accepted" if status and status < 300 else "rejected" if status and status < 500 else "errors"
            counts[bucket] += 1
            request_id = data.get("request_id") if isinstance(data, dict) and bucket == "accepted" else None
            conn.execute(
                """INSERT INTO console.job_submissions
                       (run_id, job_id, seq, request_id, http_status, payload, response_body, latency_ms, submitted_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, clock_timestamp())""",
                (run_id, job["job_id"], seq, request_id, status, Jsonb(payload), Jsonb(data), latency))

    conn.execute(
        """UPDATE console.job_runs SET finished_at = clock_timestamp(), accepted = %(accepted)s, rejected = %(rejected)s,
                  errors = %(errors)s, error = %(error)s WHERE run_id = %(run_id)s""",
        counts | {"error": run_error, "run_id": run_id})
    conn.execute(
        """UPDATE console.jobs
              SET run_count = run_count + 1,
                  total_submitted = total_submitted + %(n)s,
                  last_run_at = clock_timestamp(),
                  run_requested = false,
                  next_run_at = CASE WHEN interval_seconds IS NULL THEN NULL
                                     ELSE clock_timestamp() + make_interval(secs => interval_seconds) END,
                  status = CASE WHEN max_runs IS NOT NULL AND run_count + 1 >= max_runs THEN 'COMPLETED'
                                ELSE status END
            WHERE job_id = %(job_id)s""",
        {"n": job["requests_per_run"], "job_id": job["job_id"]})
    log.info("traffic job=%r trigger=%s accepted=%d rejected=%d errors=%d",
             job["name"], trigger, counts["accepted"], counts["rejected"], counts["errors"])


def run_traffic_jobs(conn: psycopg.Connection, http: httpx.Client, settings: SchedulerSettings) -> int:
    with conn.transaction():
        due = conn.execute(
            """SELECT j.*, s.payload AS sample_payload
               FROM console.jobs j LEFT JOIN console.payload_samples s ON s.sample_id = j.sample_id
               WHERE j.run_requested
                  OR (j.status = 'ACTIVE' AND j.interval_seconds IS NOT NULL
                      AND coalesce(j.next_run_at, now()) <= now())
               ORDER BY j.job_id
               FOR UPDATE OF j SKIP LOCKED""").fetchall()
        for job in due:
            with conn.transaction():
                run_traffic_job(conn, http, settings, job)
    return len(due)


# ----------------------------------------------------------------------------- main loop
def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = SchedulerSettings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    conn = None
    with httpx.Client(timeout=settings.http_timeout_seconds) as http:
        while not stop.is_set():
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(settings.database_url, row_factory=dict_row)
                    log.info("connected; polling jobs every %ss", settings.tick_seconds)
                run_system_jobs(conn)
                run_traffic_jobs(conn, http, settings)
            except psycopg.OperationalError:
                log.exception("database connection problem; retrying")
                conn = None
            except Exception:
                log.exception("scheduler cycle failed")
                if conn is not None and not conn.closed:
                    conn.rollback()
            stop.wait(settings.tick_seconds)
    if conn is not None:
        conn.close()
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
