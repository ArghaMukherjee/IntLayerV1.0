"""Operations console: dashboard, request explorer and test sender for the Integration Layer.

Reads PostgreSQL with the read-only ui_reader user. Anything that changes state
(submit, cancel, replay) goes through the Integration Layer API, server-side, so
API keys never reach the browser.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
STATIC = Path(__file__).parent / "static"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UI_", extra="ignore")

    database_url: str = "postgresql://ui_reader:ui_reader@localhost:5432/integration"
    il_base_url: str = "http://localhost:8000"
    il_api_key: str = ""
    admin_api_key: str = ""
    app2_url: str = "http://localhost:8001"
    app1_url: str = "http://localhost:8002"
    callback_url: str = "http://localhost:8002/hooks/il"


class SendRequest(BaseModel):
    workflow_name: str
    payload: Any
    idempotency_key: str | None = Field(default=None, max_length=200)
    use_callback: bool = True
    priority: int = 0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=5, open=False,
                                             kwargs={"autocommit": True, "row_factory": dict_row})
        await app.state.pool.open(wait=False)
        app.state.http = httpx.AsyncClient(timeout=5)
        yield
        await app.state.http.aclose()
        await app.state.pool.close()

    app = FastAPI(title="Integration Layer Console", version="1.0.0", lifespan=lifespan)

    async def fetch_all(sql: str, params: Any = None) -> list[dict]:
        async with app.state.pool.connection(timeout=5) as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def fetch_one(sql: str, params: Any = None) -> dict | None:
        async with app.state.pool.connection(timeout=5) as conn:
            return await (await conn.execute(sql, params)).fetchone()

    async def probe(name: str, url: str, extra: str | None = None) -> dict:
        result = {"name": name, "url": url, "ok": False}
        try:
            response = await app.state.http.get(url, timeout=2)
            result["ok"] = response.is_success
            result["detail"] = response.json()
            if extra:
                result["extra"] = (await app.state.http.get(extra, timeout=2)).json()
        except Exception as exc:
            result["detail"] = {"error": f"{type(exc).__name__}: {exc}"}
        return result

    async def call_il(method: str, path: str, *, admin: bool = False, **kwargs) -> JSONResponse:
        key = settings.admin_api_key if admin else settings.il_api_key
        try:
            response = await app.state.http.request(method, settings.il_base_url + path,
                                                    headers={"X-API-Key": key}, **kwargs)
        except httpx.HTTPError as exc:
            raise HTTPException(502, detail=f"Integration Layer unreachable: {exc}")
        return JSONResponse(status_code=response.status_code, content=response.json())

    # ---------------------------------------------------------------- dashboard
    @app.get("/api/overview")
    async def overview():
        kpi_sql = """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                   count(*) FILTER (WHERE status = 'FAILED') AS failed,
                   count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled,
                   count(*) FILTER (WHERE status IN ('PENDING', 'IN_PROGRESS')) AS in_flight,
                   count(*) FILTER (WHERE retry_count > 0) AS retried,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM workflow_completed_at - received_at) * 1000)
                       FILTER (WHERE status = 'COMPLETED') AS p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM workflow_completed_at - received_at) * 1000)
                       FILTER (WHERE status = 'COMPLETED') AS p95_ms
            FROM integration.request_metadata WHERE received_at >= now() - interval '24 hours'"""
        status_sql = """
            SELECT s.status, count(r.request_id) AS count
            FROM unnest(enum_range(NULL::integration.request_status)) AS s(status)
            LEFT JOIN integration.request_metadata r ON r.status = s.status
            GROUP BY s.status ORDER BY s.status"""
        queue_sql = """
            SELECT extract(epoch FROM now() - min(received_at) FILTER (WHERE status = 'PENDING')) AS oldest_pending_s,
                   count(*) FILTER (WHERE callback_status = 'PENDING') AS callbacks_pending,
                   count(*) FILTER (WHERE callback_status = 'DELIVERED') AS callbacks_delivered,
                   count(*) FILTER (WHERE callback_status = 'FAILED') AS callbacks_failed
            FROM integration.request_metadata"""
        hourly_sql = """
            SELECT h AS hour, count(r.request_id) AS received,
                   count(r.request_id) FILTER (WHERE r.status = 'COMPLETED') AS completed,
                   count(r.request_id) FILTER (WHERE r.status = 'FAILED') AS failed
            FROM generate_series(date_trunc('hour', now()) - interval '23 hours', date_trunc('hour', now()),
                                 interval '1 hour') AS h
            LEFT JOIN integration.request_metadata r ON r.received_at >= h AND r.received_at < h + interval '1 hour'
            GROUP BY h ORDER BY h"""
        kpis, statuses, queue, hourly, services = await asyncio.gather(
            fetch_one(kpi_sql), fetch_all(status_sql), fetch_one(queue_sql), fetch_all(hourly_sql),
            asyncio.gather(
                probe("Integration Layer", f"{settings.il_base_url}/health/ready"),
                probe("App2", f"{settings.app2_url}/health/ready", f"{settings.app2_url}/stats"),
                probe("App1 (mock)", f"{settings.app1_url}/health"),
            ))
        return {"kpis": kpis, "statuses": statuses, "queue": queue, "hourly": hourly, "services": services}

    # ---------------------------------------------------------------- requests
    @app.get("/api/requests")
    async def list_requests(status: str | None = None, workflow: str | None = None,
                            q: str | None = None, limit: int = Query(100, ge=1, le=500)):
        sql = ["""SELECT request_id, correlation_id, idempotency_key, source_app, target_app, workflow_name,
                         status, success_flag, workflow_status, error_code, retry_count, received_at,
                         workflow_completed_at, processing_duration_ms, callback_status,
                         (extract(epoch FROM coalesce(workflow_completed_at, now()) - received_at) * 1000)::bigint
                             AS end_to_end_ms
                  FROM integration.request_metadata WHERE true"""]
        params: dict = {"limit": limit}
        if status:
            sql.append("AND status::text = %(status)s")
            params["status"] = status
        if workflow:
            sql.append("AND workflow_name = %(workflow)s")
            params["workflow"] = workflow
        if q:
            sql.append("AND (request_id::text ILIKE %(q)s OR correlation_id::text ILIKE %(q)s"
                       " OR idempotency_key ILIKE %(q)s OR input_json::text ILIKE %(q)s)")
            params["q"] = f"%{q}%"
        sql.append("ORDER BY received_at DESC LIMIT %(limit)s")
        return await fetch_all(" ".join(sql), params)

    @app.get("/api/requests/{request_id}")
    async def request_detail(request_id: UUID):
        row = await fetch_one("SELECT * FROM integration.request_metadata WHERE request_id = %s", (request_id,))
        if row is None:
            raise HTTPException(404, detail="Request not found")
        events, calls = await asyncio.gather(
            fetch_all("SELECT * FROM integration.request_event_log WHERE request_id = %s ORDER BY event_id",
                      (request_id,)),
            fetch_all("""SELECT call_id, direction, http_method, api_url, endpoint, response_status_code,
                                latency_ms, client_ip::text, server_hostname, response_body, called_at
                         FROM integration.api_call_log WHERE request_id = %s ORDER BY call_id""", (request_id,)))
        for key in ("client_ip", "il_server_ip", "db_server_addr"):
            row[key] = str(row[key]) if row[key] is not None else None
        return {"request": row, "events": events, "api_calls": calls}

    @app.post("/api/requests")
    async def send_request(body: SendRequest):
        payload = {"workflow_name": body.workflow_name, "payload": body.payload, "priority": body.priority}
        if body.idempotency_key:
            payload["idempotency_key"] = body.idempotency_key
        if body.use_callback:
            payload["callback_url"] = settings.callback_url
        return await call_il("POST", "/v1/requests", json=payload)

    @app.post("/api/requests/{request_id}/cancel")
    async def cancel(request_id: UUID):
        return await call_il("POST", f"/v1/requests/{request_id}/cancel", params={"reason": "Cancelled from console"})

    @app.post("/api/requests/{request_id}/replay")
    async def replay(request_id: UUID):
        return await call_il("POST", f"/v1/admin/requests/{request_id}/replay", admin=True)

    # ---------------------------------------------------------------- other views
    @app.get("/api/workflows")
    async def workflows():
        return await fetch_all("SELECT * FROM integration.workflow_registry ORDER BY workflow_name")

    @app.get("/api/callbacks")
    async def callbacks():
        try:
            response = await app.state.http.get(f"{settings.app1_url}/hooks/il/received", timeout=3)
            return response.json()[:100]
        except Exception as exc:
            raise HTTPException(502, detail=f"App1 unreachable: {exc}")

    @app.get("/api/api-calls")
    async def api_calls(limit: int = Query(100, ge=1, le=500)):
        return await fetch_all("""SELECT call_id, request_id, direction, http_method, api_url, endpoint,
                                         response_status_code, latency_ms, client_ip::text, server_hostname, called_at
                                  FROM integration.api_call_log ORDER BY call_id DESC LIMIT %s""", (limit,))

    @app.get("/api/reports")
    async def reports():
        daily, watermark = await asyncio.gather(
            fetch_all("""SELECT * FROM dwh.v_daily_workflow_summary
                         WHERE full_date >= current_date - 30 ORDER BY full_date DESC, workflow_name"""),
            fetch_one("SELECT nullif(last_loaded_at, '-infinity') AS last_loaded_at, last_run_at, rows_loaded FROM dwh.etl_watermark"
                      " WHERE job_name = 'fact_request'"))
        return {"daily": daily, "watermark": watermark}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
