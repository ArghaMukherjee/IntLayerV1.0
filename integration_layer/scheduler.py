"""Maintenance scheduler (runs as its own container).

Jobs take a transaction-level advisory lock, so running more than one scheduler is safe.
"""
import logging
import signal
import threading
import time

import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("scheduler")


class SchedulerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", extra="ignore")

    database_url: str = "postgresql://il_scheduler:il_scheduler@localhost:5432/integration"
    reap_interval_seconds: int = 60
    partition_interval_seconds: int = 6 * 3600
    dwh_interval_seconds: int = 900
    tick_seconds: float = 5


def jobs(settings: SchedulerSettings) -> list[tuple[str, int, str]]:
    return [
        ("reap_expired_leases", settings.reap_interval_seconds,
         "SELECT integration.reap_expired_leases() AS result"),
        ("ensure_partitions", settings.partition_interval_seconds,
         "SELECT string_agg(integration.ensure_month_partition("
         "(current_date + make_interval(months => m))::date), ',') AS result "
         "FROM generate_series(0, 2) AS m"),
        ("dwh_load_incremental", settings.dwh_interval_seconds,
         "CALL dwh.load_incremental()"),
    ]


def run_job(conn: psycopg.Connection, name: str, sql: str) -> None:
    started = time.perf_counter()
    with conn.transaction():
        locked = conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (name,)).fetchone()[0]
        if not locked:
            log.info("job=%s skipped (running elsewhere)", name)
            return
        cur = conn.execute(sql)
        result = cur.fetchone()[0] if cur.description else None
    log.info("job=%s result=%s duration_ms=%d", name, result, (time.perf_counter() - started) * 1000)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = SchedulerSettings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    next_run = {name: 0.0 for name, _, _ in jobs(settings)}
    conn = None
    while not stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(settings.database_url)
            for name, interval, sql in jobs(settings):
                if time.monotonic() >= next_run[name]:
                    try:
                        run_job(conn, name, sql)
                    except psycopg.OperationalError:
                        raise
                    except Exception:
                        log.exception("job=%s failed", name)
                    next_run[name] = time.monotonic() + interval
        except psycopg.OperationalError:
            log.exception("Database connection problem; retrying")
            conn = None
        stop.wait(settings.tick_seconds)
    if conn is not None:
        conn.close()
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
