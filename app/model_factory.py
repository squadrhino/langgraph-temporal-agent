"""Chat models, one per agent role.

Every call goes through LiteLLM. Roles resolve to a LiteLLM alias (see
k8s/litellm.yaml), which is what decides whether a role is served by a local
vLLM engine or a hosted provider -- so switching a role to Bedrock is a change
to <ROLE>_MODEL_ID, not to this file.

There is deliberately no direct-to-provider path. A call that bypasses the
gateway also bypasses its budgets, rate limits, spend logging and metrics, which
for a hosted provider means spending money with no ceiling and no record.
"""

import os
from functools import lru_cache
from typing import Literal, TypedDict

from langchain_openai import ChatOpenAI


ModelRole = Literal[
    "supervisor",
    "order_tracking",
    "order_actions",
    "infrastructure",
]

MODEL_ROLES: tuple[ModelRole, ...] = (
    "supervisor",
    "order_tracking",
    "order_actions",
    "infrastructure",
)
LLM_TIMEOUT_SECONDS = 60


class ModelConfiguration(TypedDict):
    role: ModelRole
    model_id: str


def _role_prefix(role: ModelRole) -> str:
    return role.upper()


def model_configuration(role: ModelRole) -> ModelConfiguration:
    prefix = _role_prefix(role)
    model_id = os.getenv(f"{prefix}_MODEL_ID", os.getenv("LLM_MODEL", "")).strip()
    if not model_id:
        raise RuntimeError(
            f"{prefix}_MODEL_ID or LLM_MODEL is required for model role {role!r}"
        )
    return {"role": role, "model_id": model_id}


@lru_cache
def get_chat_model(role: ModelRole):
    configuration = model_configuration(role)
    metadata = {"agent_role": role}
    base_url = os.getenv(f"{_role_prefix(role)}_MODEL_BASE_URL", os.getenv("LLM_BASE_URL"))
    api_key = os.getenv(f"{_role_prefix(role)}_MODEL_API_KEY", os.getenv("LLM_API_KEY"))
    if not base_url or not api_key:
        raise RuntimeError(
            f"LLM_BASE_URL and LLM_API_KEY are required for model role {role!r}"
        )
    return ChatOpenAI(
        base_url=base_url,
        api_key=api_key,
        model=configuration["model_id"],
        streaming=True,
        timeout=LLM_TIMEOUT_SECONDS,
        stream_chunk_timeout=LLM_TIMEOUT_SECONDS,
        temperature=0,
        metadata=metadata,
    )
