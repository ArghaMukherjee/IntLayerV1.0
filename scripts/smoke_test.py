"""End-to-end smoke test against a running stack (docker compose or local).

    python scripts/smoke_test.py

Environment (defaults match docker-compose.yml + .env.example):
    IL_URL, APP1_URL, APP2_URL, IL_API_KEY
"""
import os
import sys
import time
import uuid

import httpx

IL_URL = os.getenv("IL_URL", "http://localhost:8000")
APP1_URL = os.getenv("APP1_URL", "http://localhost:8002")
APP2_URL = os.getenv("APP2_URL", "http://localhost:8001")
API_KEY = os.getenv("IL_API_KEY", "app1-dev-key")

il = httpx.Client(base_url=IL_URL, headers={"X-API-Key": API_KEY}, timeout=10)
failures = 0


def check(name: str, condition: bool, detail: object = "") -> None:
    global failures
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}" + ("" if condition else f"  -> {detail}"))
    failures += 0 if condition else 1


def order(order_id: str, customer: str = "CUST-001", qty: int = 2, price: float = 99.5) -> dict:
    return {"order_id": order_id, "customer_id": customer, "currency": "EUR",
            "order_date": "2026-09-28", "items": [{"sku": "SKU-1", "quantity": qty, "unit_price": price}]}


def wait_until(fn, timeout: float = 30, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(interval)
    return None


print("1. Health")
for name, url in (("IL", IL_URL), ("App2", APP2_URL), ("App1", APP1_URL)):
    check(f"{name} healthy", httpx.get(f"{url}/health").status_code == 200)
check("IL database ready", httpx.get(f"{IL_URL}/health/ready").status_code == 200)

print("2. Authentication and validation")
r = httpx.post(f"{IL_URL}/v1/requests", json={"workflow_name": "order_validation", "payload": {}})
check("missing API key -> 401", r.status_code == 401, r.text)
r = il.post("/v1/requests", json={"workflow_name": "no_such_flow", "payload": {}})
check("unknown workflow -> 422 UNKNOWN_WORKFLOW",
      r.status_code == 422 and r.json()["error"]["code"] == "UNKNOWN_WORKFLOW", r.text)
bad = {"order_id": "X", "currency": "euro", "items": [{"sku": "A", "quantity": 0, "unit_price": 1}], "extra": 1}
r = il.post("/v1/requests", json={"workflow_name": "order_validation", "payload": bad})
err = r.json().get("error", {})
paths = {e["path"] for e in err.get("errors", [])}
check("invalid payload -> 422 INPUT_SCHEMA_INVALID", r.status_code == 422 and err.get("code") == "INPUT_SCHEMA_INVALID", r.text)
check("errors point at the offending fields",
      {"$", "$.currency", "$.items[0].quantity"} <= paths, paths)

print("3. Callback mode (App1 mock -> IL -> App2 -> IL -> App1 callback)")
oid = f"ORD-{uuid.uuid4().hex[:8]}"
r = httpx.post(f"{APP1_URL}/orders", json=order(oid))
check("App1 submit accepted (202)", r.status_code == 202, r.text)
rid = r.json()["request_id"]
events = wait_until(lambda: httpx.get(f"{APP1_URL}/hooks/il/received", params={"request_id": rid}).json())
check("callback received by App1", bool(events), "no callback within 30s")
if events:
    ev = events[0]
    check("callback: COMPLETED / APPROVED",
          ev["status"] == "COMPLETED" and ev["workflow_status"] == "APPROVED" and ev["success_flag"] is True, ev)
    check("callback carries output_json", ev["output_json"]["total_amount"] == 199.0, ev["output_json"])
status = il.get(f"/v1/requests/{rid}").json()
check("IL record shows callback DELIVERED", status["callback_status"] == "DELIVERED", status)

print("4. Polling mode (business rejection)")
key = f"idem-{uuid.uuid4().hex[:8]}"
r = il.post("/v1/requests", json={"workflow_name": "order_validation",
                                  "payload": order(f"ORD-{uuid.uuid4().hex[:8]}", qty=1000, price=100),
                                  "idempotency_key": key})
check("submit -> 202", r.status_code == 202, r.text)
rid2 = r.json()["request_id"]
r = il.post("/v1/requests", json={"workflow_name": "order_validation",
                                  "payload": order("whatever"), "idempotency_key": key})
check("same idempotency_key -> 200 with original id",
      r.status_code == 200 and r.json()["request_id"] == rid2 and r.json()["is_duplicate"], r.text)
final = wait_until(lambda: (s := il.get(f"/v1/requests/{rid2}").json())["status"] == "COMPLETED" and s)
check("polled status COMPLETED / REJECTED",
      bool(final) and final["workflow_status"] == "REJECTED" and final["output_json"]["decision"] == "REJECTED",
      final)
check("processing timestamps recorded",
      bool(final) and final["picked_at"] is not None and final["processing_duration_ms"] is not None, final)

print("5. Retryable failure")
r = il.post("/v1/requests", json={"workflow_name": "order_validation",
                                  "payload": order(f"ORD-{uuid.uuid4().hex[:8]}", customer="CUST-DOWNSTREAM-DOWN")})
rid3 = r.json()["request_id"]
retried = wait_until(lambda: (s := il.get(f"/v1/requests/{rid3}").json())["retry_count"] >= 1 and s)
check("request re-queued with retry_count=1 and error_code",
      bool(retried) and retried["status"] == "PENDING" and retried["error_code"] == "DOWNSTREAM_UNAVAILABLE",
      retried)

print("6. Ownership and listing")
other = httpx.get(f"{IL_URL}/v1/requests/{uuid.uuid4()}", headers={"X-API-Key": API_KEY})
check("unknown request -> 404", other.status_code == 404, other.text)
listed = il.get("/v1/requests", params={"limit": 200}).json()
check("list includes submitted requests", {rid, rid2, rid3} <= {x["request_id"] for x in listed})
check("workflow schema is discoverable", il.get("/v1/workflows/order_validation").status_code == 200)

stats = httpx.get(f"{APP2_URL}/stats").json()
print(f"\nApp2 counters: {stats['counters']}")
print("RESULT:", "ALL PASSED" if failures == 0 else f"{failures} FAILED")
sys.exit(1 if failures else 0)
