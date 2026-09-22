# agentops

A customer-support agent that can cancel orders and issue refunds, built to work
out where the boundaries go once a model can change something.

LangGraph owns the conversation. Temporal owns anything with a consequence.
LiteLLM sits in front of every model call. All of it is declared as Kubernetes
manifests behind one Envoy Gateway.

The domain is deliberately ordinary — a refund is a readable stand-in for any
action where a model's decision becomes a durable change to a system it does
not own. **The interesting part is the boundaries, not the domain.**

The manifests describe a complete deployment, but they are a starting point
rather than a hardened one: there is no TLS, no horizontal scaling and no
backup policy, and [what this does not prove](#what-this-does-not-prove) lists
the rest. Read it before drawing conclusions from anything above it.

---

## The shape of it

```
                    Envoy Gateway  (one gateway, 8 listeners: HTTP + TCP)
                          |
  ---------------------------------------------------  stateless, disposable
   agentops-ui      agentops-api    agentops-worker    LiteLLM    vLLM x3
   (nginx/Angular)  (FastAPI)       (Temporal)         (gateway)  (engines)
  ---------------------------------------------------
                          |
  ---------------------------------------------------  stateful, system of record
   PostgreSQL              Redis                  Temporal server
   5 databases + pgvector  checkpoints, 3d TTL    event histories
  ---------------------------------------------------
```

Everything in the upper band can be killed and replaced without losing
anything. That is the property the design is organised around.

### The agent graph

One compiled `StateGraph`: a supervisor, three specialists, three tool nodes,
and two terminal guard nodes.

```
START -> supervisor -> { order_tracking | order_actions | infrastructure | invalid }
                            |         each specialist <-> its own ToolNode
                            v
                    limit | invalid | END
```

The supervisor is bound to three handoff tools that are never executed — the
graph reads the *name* of the tool chosen and resolves it through a dictionary.
Each specialist is bound only to its own tools, and its `ToolNode` is
constructed from the same list, so a specialist cannot reach another's
capability even if the model names it.

Anything with a consequence leaves the graph. `cancel_order` and
`request_refund` start Temporal workflows and return a handle, never an
outcome.

---

## Repository layout

| Path | What is in it |
|---|---|
| `app/` | FastAPI application, agent graph, tools, auth, memory |
| `app/temporal_workflows/` | Workflow and activity definitions, worker entrypoint |
| `frontend/` | Angular UI |
| `k8s/` | Every manifest: gateway, Postgres, Redis, Temporal, LiteLLM, vLLM, observability, app |
| `tests/` | 70 tests, including the boundary tests described below |
| `policies/` | Refund policy the agent is held to |
| `scripts/` | Concurrency load test against the inference stack |

---

## Running it

### Locally, against a cluster

The application needs Postgres, Redis, Temporal and LiteLLM reachable. The
manifests in `k8s/` bring those up; `.env` then points at them.

```bash
uv sync
cp .env.example .env          # then fill in LLM_API_KEY
uv run uvicorn app.main:app --port 8000
uv run python -m app.temporal_workflows.worker    # separate process
cd frontend && npm install && npm start
```

### In the cluster

Build and push the two images, then apply the manifests. Any conformant
Kubernetes cluster with the Gateway API CRDs and an Envoy Gateway controller
installed will do.

```bash
export REGISTRY=your-registry.example.com/agentops

docker build -f Dockerfile.api -t $REGISTRY/api:0.1.0 .
docker build -f Dockerfile.ui  -t $REGISTRY/ui:0.1.0  .
docker push $REGISTRY/api:0.1.0
docker push $REGISTRY/ui:0.1.0

kubectl create secret generic agentops-app-secret -n agentops \
  --from-literal=llm-api-key=<the application's LiteLLM key>

kubectl apply -f k8s/
```

`k8s/app.yaml` pins `agentops/api:0.1.0` and `agentops/ui:0.1.0`; repoint the
`image:` fields at your registry, or overlay them, before applying.

Three workloads come from those two images — the API, the Temporal worker
(same image, different command) and the UI. The gateway serves the whole
application on one hostname and splits API from UI by path, so the browser
sees a single origin and needs no CORS configuration.

The gateway publishes a `LoadBalancer` service. Point DNS for
`agentops.local`, `litellm.agentops.local`, `temporal.agentops.local`,
`grafana.agentops.local` and `keycloak.agentops.local` at its external
address, and replace the hostnames in `k8s/envoy-gateway.yaml` with names you
control. Listeners are HTTP only — terminate TLS at the gateway before
exposing any of this beyond a trusted network.

### Tests

```bash
uv run pytest
```

The manifest, workflow and recovery tests run against files and scripted
fakes, so they pass on a fresh clone with nothing else running. The rest of
`tests/test_api.py` talks to a real Postgres and Redis and **skips** when
those are not reachable — an unmet prerequisite is not a failure, and the
skip message says which service is missing. `tests/conftest.py` has the
port-forward commands.

---

## The parts worth looking at

**Capability is bound twice.** `bind_tools(...)` controls what the model is
shown; `ToolNode(...)` controls what can execute. Both take the same list.
See `app/agent_graph.py`.

**Identity never travels through the model.** Tools receive the caller via
`Annotated[str, InjectedState("user_id")]`, which is stripped from the schema
the model sees. Scoping is applied in SQL, not in the tool's judgement.

**Termination is structural.** An iteration counter and a conditional edge to a
terminal node, not an instruction asking the model to stop. The graph recursion
limit is derived from the policy (`(MAX_AGENT_ITERATIONS * 2) + 4`) so raising
one moves the other.

**Approval suspends a workflow, not a conversation.** Refunds over $200 park at
`WAITING_FOR_APPROVAL` via `workflow.wait_condition`, holding no worker and no
connection. Conversation checkpoints expire after 3 days; workflows have a
7-day execution timeout. An approval arriving on day four has to land somewhere
the conversation no longer exists.

**Idempotency lives in the database, not the workflow engine.** Temporal
guarantees at-least-once. The `UNIQUE` idempotency key on `refunds` is what
makes a second attempt harmless.

### Tests that make the model misbehave

`build_agent_graph` takes its models as arguments, so tests compile the real
graph — real edges, real routing, real `ToolNode`s — and hand it a model
scripted to emit exactly the thing that must never work.

| Model is made to | Graph must | Test |
|---|---|---|
| return an invalid handoff | stop before any specialist runs | `test_invalid_supervisor_handoff_stops_before_specialists` |
| call another specialist's tool | error, never start the workflow | `test_tracking_agent_cannot_execute_order_action_tool` |
| emit a malformed tool call | stop before any tool executes | `test_malformed_tool_call_cannot_execute_tools` |
| request tools forever | stop at three turns | `test_agent_stops_at_iteration_limit` |
| emit sensitive text | replace it before delivery | `test_unsafe_output_is_replaced_before_sse_delivery` |
| lose the worker mid-approval | accept the signal, resume, refund once | `test_approval_signal_survives_worker_restart` |
| fail after the refund commits | retry, still write one row | `test_post_commit_failure_retries_without_duplicate_refund` |

The last two are the durable side: one kills the worker mid-approval and
asserts the signal is still accepted and the refund still happens once; the
other commits a refund and then fails before acknowledging, and asserts the
retry leaves a single row.

---

## What this does not prove

- **Single node, single GPU, single worker.** No failover, no partition
  testing, no two workers disagreeing.
- **Two workflows, one of which wraps a single activity.** The per-obligation
  split is a position, not a stress-tested pattern.
- **The graph is static, single-level and sequential.** No subgraphs, no `Send`
  fan-out, no parallel nodes, no specialist-to-specialist handback.
- **The model self-check guardrail is disabled.** In its last configuration it
  blocked 100% of traffic while every health check stayed green. Deterministic
  input checks and the pre-tool state checks are on.
- **No adversarial evaluation against a live model.** Scripted stubs prove
  structural claims and say nothing about a determined attacker.
- **Spend limits are live gateway state, not configuration.** The budgeted key
  exists in the LiteLLM database, not in any manifest — a fresh cluster comes
  up with no ceiling until one is created.
- **Approval is staff-gated, not manager-gated.** `require_approver` exists in
  `app/auth.py` and is not yet wired to the route.
- **Passwords in `k8s/` are placeholders, checked in as plain Secrets.** They
  are throwaway values, reused nowhere, and must be replaced before any
  deployment that matters — ideally by an external secret store rather than
  by editing these files.
- **No TLS anywhere.** Gateway listeners are plaintext HTTP and TCP, and the
  TCP listeners exist for operator access. Terminate TLS at the gateway and
  restrict or remove those listeners before exposing this beyond a trusted
  network.

---

## Writing

A three-part series built on this repository:

1. **Graph engineering, not prompt engineering** — the graph and what holds it
   shut
2. **Three kinds of state** — agent, conversation and workflow state, and the
   rules for what crosses between them
3. **Guardrails are positions, not a product** — the layers that say no, and
   the one this platform does not have yet
