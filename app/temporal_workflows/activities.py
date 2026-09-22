from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.database import cancel_order_row, get_refund_order_row, refund_order_row
from app.temporal_workflows.models import OrderActionInput


@activity.defn
def cancel_order_activity(input: OrderActionInput) -> dict:
    return cancel_order_row(input.order_number, input.requested_by)


@activity.defn
def load_refund_order_activity(input: OrderActionInput) -> dict:
    row = get_refund_order_row(input.order_number, input.requested_by)
    if row is None:
        raise ApplicationError(
            "Order not found",
            type="OrderNotFound",
            non_retryable=True,
        )
    return {
        "id": row["id"],
        "status": row["status"],
        "amount": str(row["total"]),
    }


@activity.defn
def execute_refund_activity(input: OrderActionInput) -> dict:
    result = refund_order_row(
        input.order_number,
        input.requested_by,
        input.reason,
        idempotency_key=f"refund-order-{input.order_number}",
    )
    if result is None:
        raise ApplicationError(
            "Order not found",
            type="OrderNotFound",
            non_retryable=True,
        )
    if result.get("reason") == "order_not_delivered":
        raise ApplicationError(
            "Only delivered orders can be refunded",
            type="RefundNotAllowed",
            non_retryable=True,
        )
    return {**result, "amount": str(result["amount"])}
