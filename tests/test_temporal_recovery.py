import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from app import temporal_service
from app.database import (
    create_order_row,
    database_connection,
    initialize_database,
    refund_order_row,
)
from app.temporal_workflows.activities import (
    execute_refund_activity,
    load_refund_order_activity,
)
from app.temporal_workflows.models import OrderActionInput
from app.temporal_workflows.workflows import RefundWorkflow


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TEMPORAL_RECOVERY_TESTS") != "1",
    reason=(
        "requires RUN_TEMPORAL_RECOVERY_TESTS=1 and an explicitly configured "
        "disposable PostgreSQL database"
    ),
)


def configure_disposable_database(monkeypatch) -> None:
    database_name = os.environ.get("TEST_POSTGRES_DB", "")
    if not database_name.startswith("agentops_test_"):
        pytest.fail("TEST_POSTGRES_DB must start with 'agentops_test_'")

    required = {
        "POSTGRES_HOST": "TEST_POSTGRES_HOST",
        "POSTGRES_PORT": "TEST_POSTGRES_PORT",
        "POSTGRES_DB": "TEST_POSTGRES_DB",
        "POSTGRES_USER": "TEST_POSTGRES_USER",
        "POSTGRES_PASSWORD": "TEST_POSTGRES_PASSWORD",
    }
    for application_name, test_name in required.items():
        value = os.environ.get(test_name)
        if not value:
            pytest.fail(f"{test_name} is required for recovery tests")
        monkeypatch.setenv(application_name, value)

    initialize_database()


def create_delivered_high_value_order() -> dict:
    order = create_order_row(f"TEST-{uuid4()}", "LAP-001", 1)
    assert order is not None
    assert Decimal(order["total"]) > Decimal("200")
    with database_connection() as connection:
        connection.execute(
            "UPDATE orders SET status = 'delivered' WHERE id = %s",
            (order["id"],),
        )
    return order


def refund_database_evidence(order_number: int) -> dict:
    with database_connection() as connection:
        row = connection.execute(
            """
            SELECT o.status,
                   COUNT(r.id) AS refund_count,
                   COALESCE(MAX(r.amount), 0) AS refund_amount
            FROM orders o
            LEFT JOIN refunds r ON r.order_id = o.id
            WHERE o.id = %s
            GROUP BY o.status
            """,
            (order_number,),
        ).fetchone()
    assert row is not None
    return row


async def wait_for_approval(handle) -> dict:
    for _ in range(200):
        status = await handle.query(RefundWorkflow.status)
        if status["status"] == "WAITING_FOR_APPROVAL":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"workflow did not wait for approval; last status={status}")


def test_approval_signal_survives_worker_restart(monkeypatch) -> None:
    configure_disposable_database(monkeypatch)
    order = create_delivered_high_value_order()
    load_attempts: list[int] = []
    execute_attempts: list[int] = []

    @activity.defn(name="load_refund_order_activity")
    def tracked_load(input: OrderActionInput) -> dict:
        load_attempts.append(activity.info().attempt)
        return load_refund_order_activity(input)

    @activity.defn(name="execute_refund_activity")
    def tracked_execute(input: OrderActionInput) -> dict:
        execute_attempts.append(activity.info().attempt)
        return execute_refund_activity(input)

    async def run() -> None:
        task_queue = f"test-worker-recovery-{uuid4()}"
        workflow_id = f"refund-order-{order['id']}"
        with ThreadPoolExecutor(max_workers=4) as activity_executor:
            async with await WorkflowEnvironment.start_local(ui=False) as environment:
                monkeypatch.setattr(temporal_service, "_client", environment.client)
                monkeypatch.setattr(temporal_service, "TASK_QUEUE", task_queue)

                async with Worker(
                    environment.client,
                    task_queue=task_queue,
                    workflows=[RefundWorkflow],
                    activities=[tracked_load, tracked_execute],
                    activity_executor=activity_executor,
                ):
                    start_response = await temporal_service.start_refund(
                        order["id"],
                        order["customer_id"],
                        "recovery-test",
                    )
                    assert start_response["start_status"] == "accepted"
                    assert start_response["workflow_id"] == workflow_id
                    handle = environment.client.get_workflow_handle(
                        workflow_id,
                        run_id=start_response["run_id"],
                    )
                    waiting = await wait_for_approval(handle)
                    assert Decimal(waiting["amount"]) > Decimal("200")

                # The worker is offline here. Temporal records the signal; no
                # activity can execute until a worker starts polling again.
                await temporal_service.approve_refund(
                    workflow_id,
                    approved=True,
                    approved_by="TEST-APPROVER",
                )
                assert refund_database_evidence(order["id"]) == {
                    "status": "delivered",
                    "refund_count": 0,
                    "refund_amount": Decimal("0"),
                }

                async with Worker(
                    environment.client,
                    task_queue=task_queue,
                    workflows=[RefundWorkflow],
                    activities=[tracked_load, tracked_execute],
                    activity_executor=activity_executor,
                ):
                    result = await asyncio.wait_for(
                        handle.result(),
                        timeout=30,
                    )

        assert result["status"] == "COMPLETED"
        assert load_attempts == [1]
        assert execute_attempts == [1]

    asyncio.run(run())

    final_evidence = refund_database_evidence(order["id"])
    assert final_evidence == {
        "status": "refunded",
        "refund_count": 1,
        "refund_amount": Decimal("749.00"),
    }
    print(
        "RECOVERY_EVIDENCE worker_restart "
        f"load_attempts={load_attempts} execute_attempts={execute_attempts} "
        f"status={final_evidence['status']} "
        f"refund_count={final_evidence['refund_count']} "
        f"refund_amount={final_evidence['refund_amount']}"
    )


def test_post_commit_failure_retries_without_duplicate_refund(monkeypatch) -> None:
    configure_disposable_database(monkeypatch)
    order = create_delivered_high_value_order()
    execute_attempts: list[dict] = []

    @activity.defn(name="execute_refund_activity")
    def fail_once_after_commit(input: OrderActionInput) -> dict:
        result = refund_order_row(
            input.order_number,
            input.requested_by,
            input.reason,
            idempotency_key=f"refund-order-{input.order_number}",
        )
        assert result is not None
        attempt = activity.info().attempt
        execute_attempts.append(
            {
                "attempt": attempt,
                "refunded": result["refunded"],
                "reason": result.get("reason"),
            }
        )
        if attempt == 1:
            # Test-only failure injection: the database transaction has
            # committed, but Temporal has not received activity completion.
            raise RuntimeError("injected failure after committed refund")
        return {**result, "amount": str(result["amount"])}

    async def run() -> None:
        task_queue = f"test-refund-idempotency-{uuid4()}"
        workflow_id = f"refund-order-{order['id']}"
        with ThreadPoolExecutor(max_workers=4) as activity_executor:
            async with await WorkflowEnvironment.start_local(ui=False) as environment:
                monkeypatch.setattr(temporal_service, "_client", environment.client)
                monkeypatch.setattr(temporal_service, "TASK_QUEUE", task_queue)
                async with Worker(
                    environment.client,
                    task_queue=task_queue,
                    workflows=[RefundWorkflow],
                    activities=[load_refund_order_activity, fail_once_after_commit],
                    activity_executor=activity_executor,
                ):
                    start_response = await temporal_service.start_refund(
                        order["id"],
                        order["customer_id"],
                        "post-commit-retry-test",
                    )
                    assert start_response["start_status"] == "accepted"
                    handle = environment.client.get_workflow_handle(
                        workflow_id,
                        run_id=start_response["run_id"],
                    )
                    await wait_for_approval(handle)
                    await temporal_service.approve_refund(
                        workflow_id,
                        approved=True,
                        approved_by="TEST-APPROVER",
                    )
                    result = await handle.result()

        assert result["status"] == "COMPLETED"

    asyncio.run(run())

    assert execute_attempts == [
        {"attempt": 1, "refunded": True, "reason": None},
        {"attempt": 2, "refunded": False, "reason": "already_refunded"},
    ]
    final_evidence = refund_database_evidence(order["id"])
    assert final_evidence == {
        "status": "refunded",
        "refund_count": 1,
        "refund_amount": Decimal("749.00"),
    }
    print(
        "RECOVERY_EVIDENCE post_commit_retry "
        f"attempts={execute_attempts} status={final_evidence['status']} "
        f"refund_count={final_evidence['refund_count']} "
        f"refund_amount={final_evidence['refund_amount']}"
    )
