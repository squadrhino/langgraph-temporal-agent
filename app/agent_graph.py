import os
import re
from contextlib import asynccontextmanager
from functools import lru_cache
from time import perf_counter
from typing import Annotated, Literal

import structlog
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.checkpoint.redis.aio import AsyncRedisSaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import InjectedState, ToolNode

from app.database import (
    CANCELLABLE_ORDER_STATUSES,
    CANCELLATION_REJECTION_REASONS,
    database_counts,
    get_order_status_row,
    get_user_memories,
    save_user_memory,
)
from app.model_factory import LLM_TIMEOUT_SECONDS, get_chat_model
from app.temporal_service import start_cancellation, start_refund


logger = structlog.get_logger()

AgentName = Literal["order_tracking", "order_actions", "infrastructure"]

SUPERVISOR_MESSAGE = SystemMessage(
    content=(
        "You supervise three computer-store support agents. Choose exactly one "
        "handoff tool for every request. Use delegate_to_order_tracking for order "
        "status, saved preferences, greetings, or general support. Use "
        "delegate_to_order_actions only for explicit cancellation or refund requests. "
        "Use delegate_to_infrastructure only for read-only local API health. Never "
        "answer the user directly and never call a "
        "business tool."
    )
)

ORDER_TRACKING_MESSAGE = SystemMessage(
    content=(
        "You are the order-tracking and customer-context specialist. You may check an "
        "order status and save or retrieve preferences only when the user clearly asks. "
        "Always use get_order_status for status questions and never guess. Ask for a "
        "missing order ID. You cannot cancel or refund orders. Keep answers concise."
    )
)

ORDER_ACTIONS_MESSAGE = SystemMessage(
    content=(
        "You are the order-actions specialist. Only call cancel_order when the user "
        "clearly asks to cancel. Only call request_refund when the user clearly asks for "
        "a refund and supplies a reason. The tools start Temporal workflows; do not claim "
        "the action completed merely because a start request was accepted. If a tool "
        "returns started=false with a reason, the action was refused and no workflow is "
        "running: say plainly that it cannot be done and why, for example that a "
        "delivered order cannot be cancelled. Never tell the customer to wait for a "
        "process that was refused. You cannot look up status or perform infrastructure "
        "diagnostics. Keep answers concise."
    )
)

INFRASTRUCTURE_MESSAGE = SystemMessage(
    content=(
        "You are a read-only infrastructure diagnostics specialist. You may report the "
        "sanitized local application health summary. Never claim production health, "
        "never expose configuration or credentials, and never mutate infrastructure, "
        "orders, workflows, or deployment state. Workflow queries stay behind the "
        "staff-only API and are not available to this agent."
    )
)

SPECIALIST_MESSAGES = {
    "order_tracking": ORDER_TRACKING_MESSAGE,
    "order_actions": ORDER_ACTIONS_MESSAGE,
    "infrastructure": INFRASTRUCTURE_MESSAGE,
}

MAX_AGENT_ITERATIONS = 3
GRAPH_RECURSION_LIMIT = (MAX_AGENT_ITERATIONS * 2) + 4
# Three days, in minutes -- the checkpointer's unit.
CONVERSATION_TTL_MINUTES = 3 * 24 * 60
ITERATION_LIMIT_MESSAGE = (
    "I couldn't complete this request within the allowed number of steps."
)
INVALID_TOOL_CALL_MESSAGE = (
    "I couldn't understand the requested action. Please try again."
)


class AgentState(MessagesState):
    iteration_count: int
    user_id: str
    active_agent: str


def finish_tool_trace(
    tool_name: str,
    started_at: float,
    result: dict,
    outcome: str | None = None,
) -> dict:
    logger.info(
        "agent_tool_completed",
        tool_name=tool_name,
        outcome=outcome or result.get("error") or result.get("reason") or "success",
        duration_ms=round((perf_counter() - started_at) * 1000, 2),
    )
    return result


@tool
def get_order_status(
    order_id: str,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """Get the current status of an order using an ID such as ORD-004."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="get_order_status")
    normalized_id = order_id.strip().upper()
    match = re.fullmatch(r"ORD-(\d+)", normalized_id)
    if match is None:
        return finish_tool_trace(
            "get_order_status",
            started_at,
            {
                "found": False,
                "order_id": normalized_id,
                "error": "invalid_order_id",
            },
        )

    row = get_order_status_row(int(match.group(1)), user_id)
    if row is None:
        return finish_tool_trace(
            "get_order_status",
            started_at,
            {"found": False, "order_id": normalized_id},
            outcome="not_found",
        )

    return finish_tool_trace(
        "get_order_status",
        started_at,
        {
            "found": True,
            "order_id": f"ORD-{row['id']:03d}",
            "status": row["status"],
        },
    )


@tool
async def cancel_order(
    order_id: str,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """Start a durable order-cancellation workflow."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="cancel_order")
    normalized_id = order_id.strip().upper()
    match = re.fullmatch(r"ORD-(\d+)", normalized_id)
    if match is None:
        return finish_tool_trace(
            "cancel_order",
            started_at,
            {
                "found": False,
                "order_id": normalized_id,
                "error": "invalid_order_id",
            },
        )

    order_number = int(match.group(1))

    # Check cancellability before starting a workflow. The workflow re-checks
    # inside its transaction -- that remains the authority, since the status can
    # change in between -- but starting a run that is certain to be rejected
    # means the agent can only report "initiated" and the customer is left
    # waiting for something that already failed.
    existing = get_order_status_row(order_number, user_id)
    if existing is None:
        return finish_tool_trace(
            "cancel_order",
            started_at,
            {"found": False, "order_id": normalized_id, "error": "not_found"},
        )
    current_status = existing["status"]
    if current_status not in CANCELLABLE_ORDER_STATUSES:
        return finish_tool_trace(
            "cancel_order",
            started_at,
            {
                "found": True,
                "order_id": normalized_id,
                "cancelled": False,
                "started": False,
                "status": current_status,
                "reason": CANCELLATION_REJECTION_REASONS.get(
                    current_status, "status_not_cancellable"
                ),
            },
        )

    result = await start_cancellation(order_number, user_id)
    return finish_tool_trace(
        "cancel_order",
        started_at,
        {**result, "order_id": normalized_id},
    )


@tool
async def request_refund(
    order_id: str,
    reason: str,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """Start a durable refund workflow for an order."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="request_refund")
    normalized_id = order_id.strip().upper()
    match = re.fullmatch(r"ORD-(\d+)", normalized_id)
    if match is None:
        return finish_tool_trace(
            "request_refund",
            started_at,
            {
                "start_status": "rejected",
                "order_id": normalized_id,
                "error": "invalid_order_id",
            },
        )
    result = await start_refund(int(match.group(1)), user_id, reason.strip())
    return finish_tool_trace(
        "request_refund",
        started_at,
        {**result, "order_id": normalized_id},
    )


@tool
def save_user_preference(
    memory_key: str,
    memory_value: str,
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """Save a preference that the user explicitly asked the agent to remember."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="save_user_preference")
    row = save_user_memory(
        user_id,
        memory_key.strip().lower(),
        memory_value.strip(),
    )
    return finish_tool_trace(
        "save_user_preference",
        started_at,
        {
            "saved": True,
            "memory_key": row["memory_key"],
            "memory_value": row["memory_value"],
        },
    )


@tool
def get_user_preferences(
    user_id: Annotated[str, InjectedState("user_id")],
) -> dict:
    """Get the preferences previously saved for the current user."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="get_user_preferences")
    memories = get_user_memories(user_id)
    return finish_tool_trace(
        "get_user_preferences",
        started_at,
        {
            "preferences": {
                row["memory_key"]: row["memory_value"] for row in memories
            }
        },
    )


@tool
def get_platform_health() -> dict:
    """Return a sanitized, read-only summary of this lab API and its database."""
    started_at = perf_counter()
    logger.info("agent_tool_started", tool_name="get_platform_health")
    order_count, product_count = database_counts()
    return finish_tool_trace(
        "get_platform_health",
        started_at,
        {
            "status": "ok",
            "orders": order_count,
            "products": product_count,
            "scope": "local_application",
        },
    )


@tool
def delegate_to_order_tracking() -> str:
    """Delegate status, preference, greeting, or general support requests."""
    return "order_tracking"


@tool
def delegate_to_order_actions() -> str:
    """Delegate explicit cancellation or refund requests."""
    return "order_actions"


@tool
def delegate_to_infrastructure() -> str:
    """Delegate read-only local application-health requests."""
    return "infrastructure"


SUPERVISOR_HANDOFF_TOOLS = [
    delegate_to_order_tracking,
    delegate_to_order_actions,
    delegate_to_infrastructure,
]
HANDOFF_AGENT_BY_TOOL: dict[str, AgentName] = {
    "delegate_to_order_tracking": "order_tracking",
    "delegate_to_order_actions": "order_actions",
    "delegate_to_infrastructure": "infrastructure",
}

ORDER_TRACKING_TOOLS = [
    get_order_status,
    save_user_preference,
    get_user_preferences,
]
ORDER_ACTION_TOOLS = [cancel_order, request_refund]
INFRASTRUCTURE_TOOLS = [get_platform_health]
SPECIALIST_TOOLS = {
    "order_tracking": ORDER_TRACKING_TOOLS,
    "order_actions": ORDER_ACTION_TOOLS,
    "infrastructure": INFRASTRUCTURE_TOOLS,
}
AGENT_TOOLS = [
    *ORDER_TRACKING_TOOLS,
    *ORDER_ACTION_TOOLS,
    *INFRASTRUCTURE_TOOLS,
]


@lru_cache
def get_llm():
    """Compatibility accessor for the default order-tracking model."""
    return get_chat_model("order_tracking")


@lru_cache
def get_supervisor_llm():
    return get_chat_model("supervisor").bind_tools(SUPERVISOR_HANDOFF_TOOLS)


@lru_cache
def get_order_tracking_llm():
    return get_chat_model("order_tracking").bind_tools(ORDER_TRACKING_TOOLS)


@lru_cache
def get_order_actions_llm():
    return get_chat_model("order_actions").bind_tools(ORDER_ACTION_TOOLS)


@lru_cache
def get_infrastructure_llm():
    return get_chat_model("infrastructure").bind_tools(INFRASTRUCTURE_TOOLS)


def get_tool_enabled_llm():
    """Backward-compatible alias used by older local callers."""
    return get_order_tracking_llm()


SPECIALIST_MODEL_GETTERS = {
    "order_tracking": get_order_tracking_llm,
    "order_actions": get_order_actions_llm,
    "infrastructure": get_infrastructure_llm,
}


async def call_supervisor(state: AgentState, model=None) -> dict:
    started_at = perf_counter()
    logger.info("agent_supervisor_started", message_count=len(state["messages"]))
    model = model or get_supervisor_llm()
    try:
        response = await model.ainvoke([SUPERVISOR_MESSAGE, *state["messages"]])
    except Exception as error:
        logger.exception(
            "agent_supervisor_failed",
            error_type=type(error).__name__,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        raise

    selected_agent = "invalid"
    if not response.invalid_tool_calls and len(response.tool_calls) == 1:
        selected_agent = HANDOFF_AGENT_BY_TOOL.get(
            response.tool_calls[0]["name"],
            "invalid",
        )
    logger.info(
        "agent_supervisor_completed",
        selected_agent=selected_agent,
        duration_ms=round((perf_counter() - started_at) * 1000, 2),
    )
    return {"active_agent": selected_agent}


async def call_specialist(
    state: AgentState,
    agent_name: AgentName,
    model=None,
) -> dict:
    iteration = state.get("iteration_count", 0) + 1
    started_at = perf_counter()
    logger.info(
        "agent_model_started",
        agent_name=agent_name,
        iteration=iteration,
        message_count=len(state["messages"]),
    )
    model = model or SPECIALIST_MODEL_GETTERS[agent_name]()
    try:
        response = await model.ainvoke(
            [SPECIALIST_MESSAGES[agent_name], *state["messages"]]
        )
    except Exception as error:
        logger.exception(
            "agent_model_failed",
            agent_name=agent_name,
            iteration=iteration,
            error_type=type(error).__name__,
            duration_ms=round((perf_counter() - started_at) * 1000, 2),
        )
        raise

    if response.invalid_tool_calls:
        decision = "invalid_tool_call"
    elif response.tool_calls:
        decision = "tool_requested"
    else:
        decision = "final_answer"
    logger.info(
        "agent_model_completed",
        agent_name=agent_name,
        iteration=iteration,
        decision=decision,
        tool_names=[call["name"] for call in response.tool_calls],
        duration_ms=round((perf_counter() - started_at) * 1000, 2),
    )
    return {
        "messages": [response],
        "iteration_count": iteration,
        "active_agent": agent_name,
    }


async def iteration_limit_response(state: AgentState) -> dict:
    logger.warning(
        "agent_execution_stopped",
        reason="iteration_limit",
        agent_name=state.get("active_agent"),
        iterations=state["iteration_count"],
    )
    return {"messages": [AIMessage(content=ITERATION_LIMIT_MESSAGE)]}


async def invalid_tool_call_response(state: AgentState) -> dict:
    logger.warning(
        "agent_execution_stopped",
        reason="invalid_tool_call",
        agent_name=state.get("active_agent"),
        iterations=state.get("iteration_count", 0),
    )
    return {"messages": [AIMessage(content=INVALID_TOOL_CALL_MESSAGE)]}


def route_supervisor(state: AgentState) -> str:
    active_agent = state.get("active_agent", "invalid")
    if active_agent in SPECIALIST_TOOLS:
        return active_agent
    return "invalid"


def route_specialist_output(state: AgentState) -> str:
    latest_message = state["messages"][-1]
    if latest_message.invalid_tool_calls:
        return "invalid"
    if not latest_message.tool_calls:
        return "end"
    if state["iteration_count"] >= MAX_AGENT_ITERATIONS:
        return "limit"
    return "tools"


def build_agent_graph(
    checkpointer,
    *,
    supervisor_model=None,
    specialist_models: dict[str, object] | None = None,
):
    specialist_models = specialist_models or {}

    async def supervisor_node(state: AgentState) -> dict:
        return await call_supervisor(state, supervisor_model)

    def specialist_node(agent_name: AgentName):
        async def invoke(state: AgentState) -> dict:
            return await call_specialist(
                state,
                agent_name,
                specialist_models.get(agent_name),
            )

        return invoke

    builder = StateGraph(AgentState)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("order_tracking_agent", specialist_node("order_tracking"))
    builder.add_node("order_tracking_tools", ToolNode(ORDER_TRACKING_TOOLS))
    builder.add_node("order_actions_agent", specialist_node("order_actions"))
    builder.add_node("order_actions_tools", ToolNode(ORDER_ACTION_TOOLS))
    builder.add_node("infrastructure_agent", specialist_node("infrastructure"))
    builder.add_node("infrastructure_tools", ToolNode(INFRASTRUCTURE_TOOLS))
    builder.add_node("limit", iteration_limit_response)
    builder.add_node("invalid", invalid_tool_call_response)

    builder.add_edge(START, "supervisor")
    builder.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {
            "order_tracking": "order_tracking_agent",
            "order_actions": "order_actions_agent",
            "infrastructure": "infrastructure_agent",
            "invalid": "invalid",
        },
    )

    specialist_nodes = {
        "order_tracking": ("order_tracking_agent", "order_tracking_tools"),
        "order_actions": ("order_actions_agent", "order_actions_tools"),
        "infrastructure": ("infrastructure_agent", "infrastructure_tools"),
    }
    for agent_name, (agent_node, tool_node) in specialist_nodes.items():
        builder.add_conditional_edges(
            agent_node,
            route_specialist_output,
            {
                "tools": tool_node,
                "limit": "limit",
                "invalid": "invalid",
                "end": END,
            },
        )
        builder.add_edge(tool_node, agent_node)

    builder.add_edge("limit", END)
    builder.add_edge("invalid", END)
    return builder.compile(checkpointer=checkpointer)


@asynccontextmanager
async def agent_graph_context():
    # Conversations expire from Redis after three days. refresh_on_read means
    # the clock runs from last activity, not from when the thread started, so an
    # ongoing conversation is never cut off mid-way. What survives expiry is the
    # summary written to Postgres by app/conversation_memory.py.
    async with AsyncRedisSaver.from_conn_string(
        os.environ["REDIS_URL"],
        ttl={"default_ttl": CONVERSATION_TTL_MINUTES, "refresh_on_read": True},
    ) as checkpointer:
        await checkpointer.asetup()
        yield build_agent_graph(checkpointer)
