import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import Settings
from .processors import HANDLERS
from .queue_client import QueueClient
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=settings.db_pool_max,
                                   open=False, kwargs={"autocommit": True, "row_factory": dict_row})
        await pool.open(wait=True, timeout=30)
        client = QueueClient(pool, settings.worker_id, settings.worker_host, settings.target_app,
                             settings.lease_seconds, settings.schema_cache_seconds)
        worker = Worker(client, settings.database_url, settings.max_concurrency,
                        settings.lease_seconds, settings.poll_seconds)
        app.state.pool, app.state.worker = pool, worker
        if settings.worker_enabled:
            await worker.start()
        try:
            yield
        finally:
            await worker.stop(timeout=settings.lease_seconds)
            await pool.close()

    app = FastAPI(title="App2", version="1.0.0",
                  description="Processes integration requests from PostgreSQL.", lifespan=lifespan)
    app.state.settings = settings

    @app.get("/health", tags=["health"])
    async def liveness(request: Request):
        return {"status": "ok", "worker_id": settings.worker_id, "worker_running": request.app.state.worker.running}

    @app.get("/health/ready", tags=["health"])
    async def readiness(request: Request):
        try:
            async with request.app.state.pool.connection(timeout=3) as conn:
                await conn.execute("SELECT 1")
        except Exception as exc:
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": str(exc)})
        return {"status": "ready"}

    @app.get("/stats", tags=["worker"])
    async def stats(request: Request):
        worker: Worker = request.app.state.worker
        return {"worker_id": settings.worker_id, "running": worker.running, "started_at": worker.started_at,
                "inflight": worker.inflight, "max_concurrency": settings.max_concurrency,
                "counters": dict(worker.stats), "workflows": sorted(HANDLERS)}

    return app


app = create_app()
