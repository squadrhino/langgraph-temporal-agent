import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from app.temporal_service import TASK_QUEUE
from app.temporal_workflows.activities import (
    cancel_order_activity,
    execute_refund_activity,
    load_refund_order_activity,
)
from app.temporal_workflows.workflows import CancelOrderWorkflow, RefundWorkflow


async def main() -> None:
    client = await Client.connect(
        os.getenv("TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.getenv("TEMPORAL_NAMESPACE", "default"),
    )
    with ThreadPoolExecutor(max_workers=10) as activity_executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[CancelOrderWorkflow, RefundWorkflow],
            activities=[
                cancel_order_activity,
                load_refund_order_activity,
                execute_refund_activity,
            ],
            activity_executor=activity_executor,
        )
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
