from datetime import date
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from psycopg.types.json import Jsonb

from .. import server_info
from ..api_log import mask_headers
from ..auth import require_admin, require_app
from ..models import RequestStatus, RequestSummary, SubmitRequest, SubmitResponse

router = APIRouter(prefix="/v1/requests", tags=["requests"])
admin_router = APIRouter(prefix="/v1/admin/requests", tags=["admin"])

SUBMIT_SQL = """
SELECT * FROM integration.submit_request(
    p_source_app           => %(source_app)s,
    p_workflow_name        => %(workflow_name)s,
    p_input_json           => %(input_json)s,
    p_input_schema_version => %(schema_version)s::integer,
    p_api_url              => %(api_url)s,
    p_http_method          => %(http_method)s,
    p_request_headers      => %(request_headers)s,
    p_client_ip            => %(client_ip)s::inet,
    p_il_server_hostname   => %(hostname)s,
    p_il_server_ip         => %(server_ip)s::inet,
    p_il_instance_id       => %(instance_id)s,
    p_idempotency_key      => %(idempotency_key)s,
    p_correlation_id       => %(correlation_id)s::uuid,
    p_callback_url         => %(callback_url)s,
    p_priority             => %(priority)s::smallint)
"""


def _error(code: int, error_code: str, message: str, **extra) -> HTTPException:
    return HTTPException(code, detail={"code": error_code, "message": message, **extra})


def _check_callback_host(request: Request, url: str) -> None:
    allowed = request.app.state.settings.allowed_callback_hosts()
    host = (urlsplit(url).hostname or "").lower()
    if allowed and host not in allowed:
        raise _error(422, "CALLBACK_HOST_NOT_ALLOWED", f"callback_url host '{host}' is not allowed")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=SubmitResponse,
             responses={200: {"description": "Duplicate idempotency_key: original request returned"},
                        413: {"description": "Payload too large"},
                        422: {"description": "Payload failed JSON Schema validation"}})
async def submit_request(body: SubmitRequest, request: Request, response: Response,
                         source_app: str = Depends(require_app)):
    settings = request.app.state.settings
    size = request.headers.get("content-length")
    if size and size.isdigit() and int(size) > settings.max_payload_bytes:
        raise _error(413, "PAYLOAD_TOO_LARGE", f"Request body exceeds {settings.max_payload_bytes} bytes")

    workflow = await request.app.state.catalog.get(body.workflow_name)
    if workflow is None or not workflow.is_active:
        raise _error(422, "UNKNOWN_WORKFLOW", f"Workflow '{body.workflow_name}' is not registered or inactive")

    errors = workflow.validate_input(body.payload)
    if errors:
        raise _error(422, "INPUT_SCHEMA_INVALID", "payload does not match the workflow input schema",
                     workflow_name=workflow.workflow_name, schema_version=workflow.schema_version,
                     errors=errors)

    callback_url = str(body.callback_url) if body.callback_url else None
    if callback_url:
        _check_callback_host(request, callback_url)

    params = {
        "source_app": source_app,
        "workflow_name": workflow.workflow_name,
        "input_json": Jsonb(body.payload),
        "schema_version": workflow.schema_version,
        "api_url": str(request.url),
        "http_method": request.method,
        "request_headers": Jsonb(mask_headers(request.headers)),
        "client_ip": server_info.client_ip(request),
        "hostname": server_info.hostname(),
        "server_ip": server_info.server_ip(),
        "instance_id": settings.instance_id,
        "idempotency_key": body.idempotency_key,
        "correlation_id": body.correlation_id,
        "callback_url": callback_url,
        "priority": body.priority,
    }
    try:
        async with request.app.state.pool.connection() as conn:
            row = await (await conn.execute(SUBMIT_SQL, params)).fetchone()
    except psycopg.errors.NoDataFound:  # workflow deactivated after it was cached
        request.app.state.catalog.invalidate(workflow.workflow_name)
        raise _error(422, "UNKNOWN_WORKFLOW", f"Workflow '{body.workflow_name}' is not registered or inactive")

    request.state.request_id = row["request_id"]
    request.state.correlation_id = row["correlation_id"]
    status_url = f"/v1/requests/{row['request_id']}"
    response.headers["Location"] = status_url
    if row["is_duplicate"]:
        response.status_code = status.HTTP_200_OK
    return SubmitResponse(request_id=row["request_id"], correlation_id=row["correlation_id"],
                          status=row["status"], is_duplicate=row["is_duplicate"], status_url=status_url)


@router.get("/{request_id}", response_model=RequestStatus, response_model_exclude_none=False)
async def get_request(request_id: UUID, request: Request,
                      include_input: bool = Query(False, description="Also return input_json"),
                      source_app: str = Depends(require_app)):
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(
            "SELECT * FROM integration.v_request_status WHERE request_id = %s AND source_app = %s",
            (request_id, source_app))).fetchone()
    if row is None:
        raise _error(404, "NOT_FOUND", f"Request {request_id} not found")
    request.state.correlation_id = row["correlation_id"]
    if not include_input:
        row["input_json"] = None
    return row


@router.get("", response_model=list[RequestSummary])
async def list_requests(request: Request,
                        status_filter: str | None = Query(None, alias="status",
                                                          pattern="^(PENDING|IN_PROGRESS|COMPLETED|FAILED|CANCELLED)$"),
                        workflow_name: str | None = None,
                        from_date: date | None = None,
                        to_date: date | None = None,
                        limit: int = Query(50, ge=1, le=200),
                        offset: int = Query(0, ge=0),
                        source_app: str = Depends(require_app)):
    sql = ["SELECT * FROM integration.v_request_status WHERE source_app = %(source_app)s"]
    params = {"source_app": source_app, "limit": limit, "offset": offset}
    if status_filter:
        sql.append("AND status = %(status)s::integration.request_status")
        params["status"] = status_filter
    if workflow_name:
        sql.append("AND workflow_name = %(workflow_name)s")
        params["workflow_name"] = workflow_name
    if from_date:
        sql.append("AND request_date >= %(from_date)s")
        params["from_date"] = from_date
    if to_date:
        sql.append("AND request_date <= %(to_date)s")
        params["to_date"] = to_date
    sql.append("ORDER BY received_at DESC LIMIT %(limit)s OFFSET %(offset)s")
    async with request.app.state.pool.connection() as conn:
        return await (await conn.execute(" ".join(sql), params)).fetchall()


@router.post("/{request_id}/cancel", response_model=RequestStatus)
async def cancel_request(request_id: UUID, request: Request, reason: str | None = None,
                         source_app: str = Depends(require_app)):
    async with request.app.state.pool.connection() as conn:
        owned = await (await conn.execute(
            "SELECT 1 FROM integration.v_request_status WHERE request_id = %s AND source_app = %s",
            (request_id, source_app))).fetchone()
        if owned is None:
            raise _error(404, "NOT_FOUND", f"Request {request_id} not found")
        cancelled = (await (await conn.execute(
            "SELECT integration.cancel_request(%s, %s) AS ok", (request_id, reason))).fetchone())["ok"]
        if not cancelled:
            raise _error(409, "NOT_CANCELLABLE", "Only PENDING requests can be cancelled")
        row = await (await conn.execute(
            "SELECT * FROM integration.v_request_status WHERE request_id = %s", (request_id,))).fetchone()
    row["input_json"] = None
    return row


@admin_router.post("/{request_id}/replay", response_model=RequestStatus)
async def replay_request(request_id: UUID, request: Request, _: str = Depends(require_admin)):
    async with request.app.state.pool.connection() as conn:
        replayed = (await (await conn.execute(
            "SELECT integration.replay_request(%s) AS ok", (request_id,))).fetchone())["ok"]
        if not replayed:
            raise _error(409, "NOT_REPLAYABLE", "Only FAILED requests can be replayed")
        row = await (await conn.execute(
            "SELECT * FROM integration.v_request_status WHERE request_id = %s", (request_id,))).fetchone()
    row["input_json"] = None
    return row
