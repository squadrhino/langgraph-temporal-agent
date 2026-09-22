"""Long-term conversation memory.

Conversations live in Redis for three days (see `agent_graph.py`). What outlives
them is a short summary and its embedding, kept in Postgres so past
conversations stay searchable without retaining full transcripts.

Two calls per conversation: one to the supervisor model to write the summary,
one to the embedding model to vectorise it.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import redis.asyncio as redis
import structlog
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.database import save_conversation_summary, search_conversation_summaries
from app.model_factory import get_chat_model


logger = structlog.get_logger()

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL_ID", "embedding-model")
EMBEDDING_TIMEOUT_SECONDS = 30

# A conversation nobody has added to for this long is treated as finished.
IDLE_TIMEOUT_SECONDS = int(os.getenv("CONVERSATION_IDLE_SECONDS", "300"))
SWEEP_INTERVAL_SECONDS = 60
# Sorted set of "customer_id:thread_id" scored by last-activity epoch seconds.
# A ZSET rather than one key per thread so finding idle ones is a single ranged
# read instead of a scan.
ACTIVITY_KEY = "conversation_activity"

SUMMARY_PROMPT = SystemMessage(
    content=(
        "Summarise this customer support conversation in two or three sentences. "
        "State what the customer wanted, what was done, and how it ended. Use "
        "plain past tense, no greetings, no bullet points. Mention any order IDs. "
        "If nothing of substance happened, say so briefly."
    )
)


def _transcript(messages: list) -> tuple[str, int]:
    """Render the thread as plain text, ignoring tool chatter."""
    lines: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "Customer"
        elif isinstance(message, AIMessage):
            role = "Agent"
        else:
            continue
        text = message.content if isinstance(message.content, str) else ""
        if text.strip():
            lines.append(f"{role}: {text.strip()}")
    return "\n".join(lines), len(lines)


async def embed(text: str) -> list[float]:
    """Vectorise one piece of text via the LiteLLM embeddings endpoint."""
    base_url = os.environ["LLM_BASE_URL"].rstrip("/")
    async with httpx.AsyncClient(timeout=EMBEDDING_TIMEOUT_SECONDS) as client:
        response = await client.post(
            f"{base_url}/embeddings",
            headers={"Authorization": f"Bearer {os.environ['LLM_API_KEY']}"},
            json={"model": EMBEDDING_MODEL, "input": text},
        )
        response.raise_for_status()
        return response.json()["data"][0]["embedding"]


async def summarise_and_store(
    agent_graph,
    customer_id: str,
    thread_id: str,
) -> dict:
    """Summarise a finished conversation and store it with its embedding.

    Returns a small result dict rather than raising when there is nothing to
    summarise: ending an empty conversation is not an error.
    """
    config = {"configurable": {"thread_id": f"{customer_id}:{thread_id}"}}
    state = await agent_graph.aget_state(config)
    messages = (state.values or {}).get("messages", []) if state else []

    transcript, message_count = _transcript(messages)
    if not transcript:
        return {"stored": False, "reason": "empty_conversation"}

    model = get_chat_model("supervisor")
    reply = await model.ainvoke([SUMMARY_PROMPT, HumanMessage(content=transcript)])
    summary = (reply.content or "").strip() if isinstance(reply.content, str) else ""
    if not summary:
        return {"stored": False, "reason": "empty_summary"}

    vector = await embed(summary)
    save_conversation_summary(customer_id, thread_id, summary, vector, message_count)
    logger.info(
        "conversation_summarised",
        thread_id=thread_id,
        message_count=message_count,
        summary_length=len(summary),
    )
    return {
        "stored": True,
        "thread_id": thread_id,
        "summary": summary,
        "message_count": message_count,
    }


async def search(customer_id: str, query: str, limit: int = 5) -> list[dict]:
    """Find this customer's past conversations by meaning, not keywords."""
    vector = await embed(query)
    rows = search_conversation_summaries(customer_id, vector, limit)
    return [
        {
            "threadId": row["thread_id"],
            "summary": row["summary"],
            "messageCount": row["message_count"],
            "createdAt": row["created_at"],
            "similarity": round(float(row["similarity"]), 4),
        }
        for row in rows
    ]


def _redis() -> redis.Redis:
    return redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


async def touch(customer_id: str, thread_id: str) -> None:
    """Record activity on a conversation. Called on every turn."""
    client = _redis()
    try:
        await client.zadd(ACTIVITY_KEY, {f"{customer_id}:{thread_id}": time.time()})
    finally:
        await client.aclose()


async def sweep_idle(agent_graph) -> int:
    """Summarise conversations idle past the timeout. Returns how many."""
    cutoff = time.time() - IDLE_TIMEOUT_SECONDS
    client = _redis()
    try:
        stale = await client.zrangebyscore(ACTIVITY_KEY, 0, cutoff)
        closed = 0
        for entry in stale:
            customer_id, _, thread_id = entry.partition(":")
            if not thread_id:
                await client.zrem(ACTIVITY_KEY, entry)
                continue
            try:
                result = await summarise_and_store(agent_graph, customer_id, thread_id)
                if result.get("stored"):
                    closed += 1
            except Exception as error:
                # One bad conversation must not stall the sweep; drop the marker
                # so a permanently broken thread cannot be retried forever.
                logger.warning(
                    "conversation_sweep_failed",
                    thread_id=thread_id,
                    error_type=type(error).__name__,
                )
            await client.zrem(ACTIVITY_KEY, entry)
        return closed
    finally:
        await client.aclose()


async def sweep_loop(agent_graph) -> None:
    """Background task: close idle conversations on a fixed interval."""
    logger.info(
        "conversation_sweeper_started",
        idle_timeout_seconds=IDLE_TIMEOUT_SECONDS,
        interval_seconds=SWEEP_INTERVAL_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            closed = await sweep_idle(agent_graph)
            if closed:
                logger.info("conversations_closed", count=closed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("conversation_sweep_error", error_type=type(error).__name__)
