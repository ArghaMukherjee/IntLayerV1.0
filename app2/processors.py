"""Business handlers, one per workflow_name.

A handler receives the validated input_json and returns a Result. Raise
RetryableError for temporary problems (retried with backoff) and PermanentError
for problems that retrying will not fix. Any other exception is treated as retryable.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal


class RetryableError(Exception):
    def __init__(self, message: str, code: str = "RETRYABLE_ERROR"):
        super().__init__(message)
        self.code = code


class PermanentError(Exception):
    def __init__(self, message: str, code: str = "PERMANENT_ERROR"):
        super().__init__(message)
        self.code = code


@dataclass
class Result:
    output: dict
    workflow_status: str = "COMPLETED"


Handler = Callable[[dict], Awaitable[Result]]
HANDLERS: dict[str, Handler] = {}


def handler(workflow_name: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[workflow_name] = fn
        return fn
    return register


# --- order_validation ---------------------------------------------------------

CREDIT_LIMIT = Decimal("50000")
MAX_QUANTITY_PER_LINE = 1000
# Demo hook: simulates the downstream credit service being unavailable
UNAVAILABLE_CUSTOMER = "CUST-DOWNSTREAM-DOWN"


@handler("order_validation")
async def order_validation(order: dict) -> Result:
    if order["customer_id"] == UNAVAILABLE_CUSTOMER:
        raise RetryableError("Credit check service unavailable", code="DOWNSTREAM_UNAVAILABLE")

    total = sum(Decimal(str(i["unit_price"])) * i["quantity"] for i in order["items"])
    reasons = []
    if total > CREDIT_LIMIT:
        reasons.append(f"Order total {total} exceeds credit limit {CREDIT_LIMIT}")
    reasons += [f"Quantity {i['quantity']} for SKU {i['sku']} exceeds {MAX_QUANTITY_PER_LINE}"
                for i in order["items"] if i["quantity"] > MAX_QUANTITY_PER_LINE]

    decision = "REJECTED" if reasons else "APPROVED"
    return Result(
        output={"order_id": order["order_id"], "decision": decision,
                "total_amount": float(total), "currency": order["currency"], "reasons": reasons},
        workflow_status=decision,
    )
