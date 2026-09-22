import json
import re
from collections.abc import AsyncIterator
from time import perf_counter

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel, Field

from app.agent_graph import GRAPH_RECURSION_LIMIT
from app.auth import CurrentUser, get_current_user, oidc_settings, require_staff, require_user
from app.conversation_memory import search as search_conversations
from app.conversation_memory import summarise_and_store
from app.conversation_memory import touch as touch_conversation
from app.guardrails import (
    INPUT_BLOCKED_MESSAGE,
    OUTPUT_BLOCKED_MESSAGE,
    GuardrailViolation,
    validate_input,
    validate_output,
)
from app.nemo_guardrails import (
    check_nemo_input,
    check_nemo_output,
    nemo_guardrails_enabled,
)
from app.temporal_service import approve_refund, get_workflow_status


router = APIRouter(tags=["agent"])
logger = structlog.get_logger()
USER_RESPONSE_GRAPH_NODES = {
    "order_tracking_agent",
    "order_actions_agent",
    "infrastructure_agent",
    "limit",
    "invalid",
}

DONE_EVENT = "event: done\ndata: {}\n\n"


# Reasoning models (Qwen3 and others) wrap internal deliberation in <think>
# blocks. Thinking is disabled per-role in the LiteLLM config, so this is a
# second line of defence: a model that emits it anyway must not have it shown
# to a customer. An unclosed block means the response was cut off mid-thought,
# so everything from the opening tag is dropped too.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>.*$", re.DOTALL | re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    cleaned = _THINK_BLOCK.sub("", text)
    cleaned = _THINK_OPEN.sub("", cleaned)
    return cleaned.strip()


class AgentRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class AgentStreamRequest(AgentRequest):
    user_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    thread_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class AgentResponse(BaseModel):
    message: str


class ApprovalRequest(BaseModel):
    approved: bool


@router.post("/agent", response_model=AgentResponse)
async def talk_to_agent(request: AgentRequest) -> AgentResponse:
    logger.info("agent_request_received", message_length=len(request.message))
    return AgentResponse(
        message="Agent endpoint is ready. LLM integration is not configured yet."
    )


async def stream_llm_response(
    user_message: str,
    user_id: str,
    thread_id: str,
    agent_graph,
) -> AsyncIterator[str]:
    started_at = perf_counter()
    logger.info("agent_execution_started", thread_id=thread_id)
    try:
        try:
            validate_input(user_message)
        except GuardrailViolation as error:
            logger.warning("agent_input_blocked", reason=error.reason)
            data = json.dumps({"content": INPUT_BLOCKED_MESSAGE})
            yield f"event: message\ndata: {data}\n\n"
            yield DONE_EVENT
            return
        if nemo_guardrails_enabled() and not await check_nemo_input(user_message):
            logger.warning("agent_input_blocked", reason="nemo_guardrail")
            data = json.dumps({"content": INPUT_BLOCKED_MESSAGE})
            yield f"event: message\ndata: {data}\n\n"
            yield DONE_EVENT
            return

        config = {
            "configurable": {"thread_id": f"{user_id}:{thread_id}"},
            "recursion_limit": GRAPH_RECURSION_LIMIT,
        }
        response_parts = []
        async for event in agent_graph.astream(
            {
                "messages": [HumanMessage(content=user_message)],
                "iteration_count": 0,
                "user_id": user_id,
            },
            config=config,
            stream_mode="messages",
            version="v2",
        ):
            if event["type"] != "messages":
                continue
            message_chunk, metadata = event["data"]
            if not isinstance(message_chunk, AIMessage):
                continue
            graph_node = metadata.get("langgraph_node") if metadata else None
            if graph_node and graph_node not in USER_RESPONSE_GRAPH_NODES:
                continue
            if isinstance(message_chunk.content, str) and message_chunk.content:
                response_parts.append(message_chunk.content)

        response_text = strip_reasoning("".join(response_parts))
        try:
            validate_output(response_text)
        except GuardrailViolation as error:
            logger.warning("agent_output_blocked", reason=error.reason)
            response_text = OUTPUT_BLOCKED_MESSAGE

        if (
            response_text != OUTPUT_BLOCKED_MESSAGE
            and nemo_guardrails_enabled()
            and not await check_nemo_output(response_text)
        ):
            logger.warning("agent_output_blocked", reason="nemo_guardrail")
            response_text = OUTPUT_BLOCKED_MESSAGE

        if response_text:
            data = json.dumps({"content": response_text})
            yield f"event: message\ndata: {data}\n\n"
    except Exception as error:
        logger.exception("agent_stream_failed", error_type=type(error).__name__)
        data = json.dumps({"message": "The language model is currently unavailable."})
        yield f"event: error\ndata: {data}\n\n"
    finally:
        logger.info(
            "agent_execution_completed",
            thread_id=thread_id,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
    # Not inside `finally`: yielding there breaks when the client disconnects.
    yield DONE_EVENT


@router.post("/agent/chat/stream")
async def stream_agent_chat(
    request: AgentStreamRequest,
    http_request: Request,
    user: CurrentUser | None = Depends(get_current_user),
) -> StreamingResponse:
    logger.info("agent_stream_request_received", message_length=len(request.message))
    # The agent's order tools filter on customer_id, so that is the identity the
    # graph needs -- not user_id, which is the Keycloak subject UUID.
    #
    # Staff have no customer_id claim: they are not a customer, so there is no
    # single customer whose orders the conversation is about. Until the UI lets
    # them pick one, they are refused here rather than silently scoped to
    # nothing. request.user_id is only honoured for anonymous callers, which
    # exists so the lab still works with OIDC_REQUIRED=false.
    if user is not None:
        if user.customer_id is None:
            raise HTTPException(
                status_code=403,
                detail=(
                    "This account has no customer_id claim. The support agent "
                    "acts on behalf of a single customer; staff accounts must "
                    "use the operations console."
                ),
            )
        user_id = user.customer_id
    elif oidc_settings()["required"]:
        # Without this the body's user_id is honoured for anonymous callers,
        # which lets anyone read any customer's orders through the agent simply
        # by naming them. The fallback exists only for OIDC_REQUIRED=false.
        raise HTTPException(status_code=401, detail="Authentication required")
    else:
        user_id = request.user_id
    # Marks the conversation active so the idle sweeper knows when it ended.
    await touch_conversation(user_id, request.thread_id)
    return StreamingResponse(
        stream_llm_response(
            request.message,
            user_id,
            request.thread_id,
            http_request.app.state.agent_graph,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/agent/workflows/{workflow_id}")
async def workflow_status(
    workflow_id: str,
    _: CurrentUser = Depends(require_staff),
) -> dict:
    return await get_workflow_status(workflow_id)


@router.post("/agent/workflows/{workflow_id}/approval", status_code=202)
async def workflow_approval(
    workflow_id: str,
    request: ApprovalRequest,
    staff: CurrentUser = Depends(require_staff),
) -> dict:
    # The approver comes from the verified identity, never the request body.
    await approve_refund(workflow_id, request.approved, staff.user_id)
    return {"workflow_id": workflow_id, "signal_sent": True}


def _customer_scope(user: CurrentUser) -> str:
    """Conversations belong to a customer, so staff have no scope of their own."""
    if user.customer_id is None:
        raise HTTPException(
            status_code=403,
            detail="This account has no customer_id claim.",
        )
    return user.customer_id


@router.post("/agent/chat/{thread_id}/end")
async def end_conversation(
    thread_id: str,
    http_request: Request,
    user: CurrentUser = Depends(require_user),
) -> dict:
    """Close a conversation: summarise it and keep the summary searchable.

    The Redis checkpoint still expires on its own after three days; this is what
    makes the conversation findable afterwards.
    """
    return await summarise_and_store(
        http_request.app.state.agent_graph,
        _customer_scope(user),
        thread_id,
    )


@router.get("/agent/conversations/search")
async def conversation_search(
    q: str,
    limit: int = 5,
    user: CurrentUser = Depends(require_user),
) -> dict:
    """Semantic search over this customer's past conversations."""
    results = await search_conversations(_customer_scope(user), q, min(limit, 20))
    return {"query": q, "results": results}
