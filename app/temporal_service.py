import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from app.temporal_workflows.models import ApprovalInput, OrderActionInput
from app.temporal_workflows.workflows import CancelOrderWorkflow, RefundWorkflow


TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "order-workflows")
WORKFLOW_TIMEOUT = timedelta(days=7)
_client: Client | None = None


async def initialize_temporal_client() -> Client:
    global _client
    _client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
        lazy=True,
    )
    return _client


def get_temporal_client() -> Client:
    if _client is None:
        raise RuntimeError("Temporal client is not initialized")
    return _client


async def start_cancellation(order_number: int, requested_by: str) -> dict:
    return await _start_workflow(
        CancelOrderWorkflow.run,
        OrderActionInput(order_number=order_number, requested_by=requested_by),
        workflow_id=f"cancel-order-{order_number}",
    )


async def start_refund(order_number: int, requested_by: str, reason: str) -> dict:
    return await _start_workflow(
        RefundWorkflow.run,
        OrderActionInput(
            order_number=order_number,
            requested_by=requested_by,
            reason=reason,
        ),
        workflow_id=f"refund-order-{order_number}",
    )


async def _start_workflow(workflow, input: OrderActionInput, workflow_id: str) -> dict:
    # A closed workflow may run again (e.g. a refund retried after delivery).
    # The database keeps this safe: refunds and cancellations are idempotent.
    handle = await get_temporal_client().start_workflow(
        workflow,
        input,
        id=workflow_id,
        task_queue=TASK_QUEUE,
        execution_timeout=WORKFLOW_TIMEOUT,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
    return {
        # With USE_EXISTING, the public SDK handle does not say whether this
        # call created the execution or reused the running one. "accepted" is
        # accurate for both and avoids a racy describe-then-start check.
        "start_status": "accepted",
        "workflow_id": handle.id,
        "run_id": handle.result_run_id,
    }


async def get_workflow_status(workflow_id: str) -> dict:
    handle = get_temporal_client().get_workflow_handle(workflow_id)
    return await handle.query("status")


async def approve_refund(
    workflow_id: str,
    approved: bool,
    approved_by: str,
) -> None:
    handle = get_temporal_client().get_workflow_handle(workflow_id)
    await handle.signal(
        "approve",
        ApprovalInput(approved=approved, approved_by=approved_by),
    )
