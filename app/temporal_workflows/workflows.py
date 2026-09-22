from datetime import timedelta
from decimal import Decimal

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.temporal_workflows.activities import (
        cancel_order_activity,
        execute_refund_activity,
        load_refund_order_activity,
    )
    from app.temporal_workflows.models import ApprovalInput, OrderActionInput


ACTIVITY_TIMEOUT = timedelta(seconds=10)
REFUND_APPROVAL_THRESHOLD = Decimal("200")
ACTIVITY_RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    non_retryable_error_types=["OrderNotFound", "RefundNotAllowed"],
)


@workflow.defn
class CancelOrderWorkflow:
    def __init__(self) -> None:
        self._state = {"status": "STARTED"}

    @workflow.run
    async def run(self, input: OrderActionInput) -> dict:
        self._state = {"status": "CANCELLING"}
        try:
            result = await workflow.execute_activity(
                cancel_order_activity,
                input,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            self._state = {"status": "COMPLETED", "result": result}
            return self._state
        except Exception:
            self._state = {"status": "FAILED"}
            raise

    @workflow.query
    def status(self) -> dict:
        return self._state


@workflow.defn
class RefundWorkflow:
    def __init__(self) -> None:
        self._state = {"status": "STARTED"}
        self._approval: ApprovalInput | None = None

    @workflow.run
    async def run(self, input: OrderActionInput) -> dict:
        try:
            self._state = {"status": "CHECKING_ORDER"}
            order = await workflow.execute_activity(
                load_refund_order_activity,
                input,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )

            if order["status"] == "refunded":
                self._state = {"status": "COMPLETED", "result": "already_refunded"}
                return self._state
            if order["status"] != "delivered":
                self._state = {"status": "REJECTED", "reason": "order_not_delivered"}
                return self._state

            if Decimal(order["amount"]) > REFUND_APPROVAL_THRESHOLD:
                self._state = {
                    "status": "WAITING_FOR_APPROVAL",
                    "amount": order["amount"],
                }
                await workflow.wait_condition(lambda: self._approval is not None)
                if not self._approval.approved:
                    self._state = {
                        "status": "REJECTED",
                        "reason": "approval_denied",
                    }
                    return self._state

            self._state = {"status": "REFUNDING", "amount": order["amount"]}
            result = await workflow.execute_activity(
                execute_refund_activity,
                input,
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            self._state = {"status": "COMPLETED", "result": result}
            return self._state
        except Exception:
            self._state = {"status": "FAILED"}
            raise

    @workflow.signal
    def approve(self, input: ApprovalInput) -> None:
        self._approval = input

    @workflow.query
    def status(self) -> dict:
        return self._state
