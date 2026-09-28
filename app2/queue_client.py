"""App2's only interface to the integration database: four SQL functions and a schema lookup."""
import time
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


@dataclass
class Job:
    request_id: UUID
    request_date: date
    correlation_id: UUID
    workflow_name: str
    input_json: dict
    retry_count: int


class QueueClient:
    def __init__(self, pool: AsyncConnectionPool, worker_id: str, worker_host: str,
                 target_app: str, lease_seconds: int, schema_cache_seconds: float = 60):
        self._pool = pool
        self.worker_id = worker_id
        self._worker_host = worker_host
        self._target_app = target_app
        self._lease = lease_seconds
        self._schema_ttl = schema_cache_seconds
        self._schemas: dict[str, tuple[float, Draft202012Validator | None]] = {}

    async def claim(self, limit: int) -> list[Job]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT * FROM integration.claim_requests(%s, %s, %s, %s, %s)",
                (self.worker_id, self._worker_host, self._target_app, limit, self._lease))).fetchall()
        return [Job(**row) for row in rows]

    async def heartbeat(self, job: Job) -> bool:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT integration.heartbeat(%s, %s, %s) AS ok",
                (job.request_id, self.worker_id, self._lease))).fetchone()
        return bool(row["ok"])

    async def complete(self, job: Job, output: dict, workflow_status: str) -> bool:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT integration.complete_request(%s, %s, %s, %s) AS ok",
                (job.request_id, self.worker_id, Jsonb(output), workflow_status))).fetchone()
        return bool(row["ok"])

    async def fail(self, job: Job, code: str, message: str, retryable: bool,
                   output: dict | None = None, workflow_status: str = "FAILED") -> str | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT integration.fail_request(%s, %s, %s, %s, %s, %s, %s) AS status",
                (job.request_id, self.worker_id, code, message[:4000], retryable,
                 Jsonb(output) if output is not None else None, workflow_status))).fetchone()
        return row["status"]

    async def output_validator(self, workflow_name: str) -> Draft202012Validator | None:
        cached = self._schemas.get(workflow_name)
        if cached and time.monotonic() - cached[0] < self._schema_ttl:
            return cached[1]
        async with self._pool.connection() as conn:
            row = await (await conn.execute(
                "SELECT output_schema FROM integration.workflow_registry WHERE workflow_name = %s",
                (workflow_name,))).fetchone()
        schema = row["output_schema"] if row else None
        validator = Draft202012Validator(schema, format_checker=FormatChecker()) if schema else None
        self._schemas[workflow_name] = (time.monotonic(), validator)
        return validator
