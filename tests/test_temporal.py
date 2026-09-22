import asyncio
from uuid import uuid4

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import temporal_service
from app.temporal_workflows.models import ApprovalInput, OrderActionInput
from app.temporal_workflows.workflows import CancelOrderWorkflow, RefundWorkflow


def test_cancellation_runs_as_a_temporal_workflow() -> None:
    async def run() -> None:
        @activity.defn(name="cancel_order_activity")
        async def cancel(_: OrderActionInput) -> dict:
            return {
                "found": True,
                "order_id": "ORD-0004",
                "cancelled": True,
                "status": "cancelled",
                "previous_status": "paid",
                "reason": "cancelled",
            }

        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue="test-cancellation",
                workflows=[CancelOrderWorkflow],
                activities=[cancel],
            ):
                result = await environment.client.execute_workflow(
                    CancelOrderWorkflow.run,
                    OrderActionInput(4, "CUS-004"),
                    id="test-cancellation",
                    task_queue="test-cancellation",
                )

        assert result["status"] == "COMPLETED"
        assert result["result"] == {
            "found": True,
            "order_id": "ORD-0004",
            "cancelled": True,
            "status": "cancelled",
            "previous_status": "paid",
            "reason": "cancelled",
        }

    asyncio.run(run())


def test_refund_at_approval_threshold_completes_automatically() -> None:
    async def run() -> None:
        @activity.defn(name="load_refund_order_activity")
        async def load_order(_: OrderActionInput) -> dict:
            return {"id": 5, "status": "delivered", "amount": "200.00"}

        @activity.defn(name="execute_refund_activity")
        async def execute_refund(_: OrderActionInput) -> dict:
            return {"id": 5, "status": "refunded", "amount": "200.00"}

        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue="test-refund-small",
                workflows=[RefundWorkflow],
                activities=[load_order, execute_refund],
            ):
                result = await environment.client.execute_workflow(
                    RefundWorkflow.run,
                    OrderActionInput(5, "CUS-005", "damaged"),
                    id="test-refund-small",
                    task_queue="test-refund-small",
                )

        assert result["status"] == "COMPLETED"
        assert result["result"]["status"] == "refunded"

    asyncio.run(run())


def test_large_refund_waits_for_approval() -> None:
    async def run() -> None:
        @activity.defn(name="load_refund_order_activity")
        async def load_order(_: OrderActionInput) -> dict:
            return {"id": 19, "status": "delivered", "amount": "749.00"}

        @activity.defn(name="execute_refund_activity")
        async def execute_refund(_: OrderActionInput) -> dict:
            return {"id": 19, "status": "refunded", "amount": "749.00"}

        async with await WorkflowEnvironment.start_time_skipping() as environment:
            async with Worker(
                environment.client,
                task_queue="test-refund-approval",
                workflows=[RefundWorkflow],
                activities=[load_order, execute_refund],
            ):
                handle = await environment.client.start_workflow(
                    RefundWorkflow.run,
                    OrderActionInput(19, "CUS-019", "damaged"),
                    id="test-refund-approval",
                    task_queue="test-refund-approval",
                )

                for _ in range(100):
                    status = await handle.query(RefundWorkflow.status)
                    if status["status"] == "WAITING_FOR_APPROVAL":
                        break
                    await asyncio.sleep(0.01)
                assert status["status"] == "WAITING_FOR_APPROVAL"

                await handle.signal(
                    RefundWorkflow.approve,
                    ApprovalInput(approved=True, approved_by="STAFF-001"),
                )
                result = await handle.result()

        assert result["status"] == "COMPLETED"

    asyncio.run(run())


def test_running_duplicate_workflow_returns_neutral_accepted_response(monkeypatch) -> None:
    async def run() -> None:
        @activity.defn(name="load_refund_order_activity")
        async def load_order(_: OrderActionInput) -> dict:
            return {"id": 19, "status": "delivered", "amount": "749.00"}

        @activity.defn(name="execute_refund_activity")
        async def execute_refund(_: OrderActionInput) -> dict:
            return {"id": 19, "status": "refunded", "amount": "749.00"}

        task_queue = f"test-running-duplicate-{uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            monkeypatch.setattr(temporal_service, "_client", environment.client)
            monkeypatch.setattr(temporal_service, "TASK_QUEUE", task_queue)
            async with Worker(
                environment.client,
                task_queue=task_queue,
                workflows=[RefundWorkflow],
                activities=[load_order, execute_refund],
            ):
                first = await temporal_service.start_refund(
                    19,
                    "CUS-019",
                    "damaged",
                )
                handle = environment.client.get_workflow_handle(first["workflow_id"])
                for _ in range(100):
                    status = await handle.query(RefundWorkflow.status)
                    if status["status"] == "WAITING_FOR_APPROVAL":
                        break
                    await asyncio.sleep(0.01)
                assert status["status"] == "WAITING_FOR_APPROVAL"

                duplicate = await temporal_service.start_refund(
                    19,
                    "CUS-019",
                    "damaged",
                )

                assert first == {
                    "start_status": "accepted",
                    "workflow_id": "refund-order-19",
                    "run_id": first["run_id"],
                }
                assert first["run_id"]
                assert duplicate == first

                await handle.signal(
                    RefundWorkflow.approve,
                    ApprovalInput(approved=False, approved_by="TEST-APPROVER"),
                )
                assert (await handle.result())["status"] == "REJECTED"

    asyncio.run(run())


def test_closed_workflow_id_can_run_again(monkeypatch) -> None:
    async def run() -> None:
        @activity.defn(name="load_refund_order_activity")
        async def load_order(_: OrderActionInput) -> dict:
            return {"id": 5, "status": "delivered", "amount": "200.00"}

        @activity.defn(name="execute_refund_activity")
        async def execute_refund(_: OrderActionInput) -> dict:
            return {"id": 5, "status": "refunded", "amount": "200.00"}

        task_queue = f"test-closed-duplicate-{uuid4()}"
        async with await WorkflowEnvironment.start_time_skipping() as environment:
            monkeypatch.setattr(temporal_service, "_client", environment.client)
            monkeypatch.setattr(temporal_service, "TASK_QUEUE", task_queue)
            async with Worker(
                environment.client,
                task_queue=task_queue,
                workflows=[RefundWorkflow],
                activities=[load_order, execute_refund],
            ):
                first = await temporal_service.start_refund(
                    5,
                    "CUS-005",
                    "damaged",
                )
                handle = environment.client.get_workflow_handle(first["workflow_id"])
                assert (await handle.result())["status"] == "COMPLETED"

                duplicate = await temporal_service.start_refund(
                    5,
                    "CUS-005",
                    "damaged",
                )

                assert duplicate["start_status"] == "accepted"
                assert duplicate["workflow_id"] == "refund-order-5"
                assert duplicate["run_id"] != first["run_id"]

    asyncio.run(run())
