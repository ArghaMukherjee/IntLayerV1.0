from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def liveness(request: Request):
    return {"status": "ok", "instance_id": request.app.state.settings.instance_id}


@router.get("/health/ready")
async def readiness(request: Request):
    try:
        async with request.app.state.pool.connection(timeout=3) as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": str(exc)})
    return {"status": "ready", "database": "ok"}
