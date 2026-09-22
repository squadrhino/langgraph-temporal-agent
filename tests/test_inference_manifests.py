from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MODEL_NAMES = (
    "vllm-qwen-agent",
    "vllm-qwen-specialist",
    "vllm-embedding",
)

# The generative engines. The embedding engine runs in pooling mode and is
# excluded wherever an assertion is about chat or tool calling.
GENERATIVE_NAMES = ("vllm-qwen-agent", "vllm-qwen-specialist")


def load_documents(name: str) -> list[dict]:
    with (ROOT / "k8s" / name).open(encoding="utf-8") as manifest:
        return [document for document in yaml.safe_load_all(manifest) if document]


def resource(documents: list[dict], kind: str, name: str) -> dict:
    return next(
        document
        for document in documents
        if document["kind"] == kind and document["metadata"]["name"] == name
    )


def assert_safe_gpu_budget(values: list[float], maximum: float = 0.90) -> None:
    total = sum(values)
    assert total <= maximum, f"vLLM processes reserve {total:.2f} of one GPU"


def test_vllm_manifest_has_three_private_time_sliced_deployments() -> None:
    documents = load_documents("vllm.yaml")
    config = resource(documents, "ConfigMap", "vllm-model-config")["data"]

    for name in MODEL_NAMES:
        service = resource(documents, "Service", name)
        deployment = resource(documents, "Deployment", name)
        pod_spec = deployment["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]

        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["ports"] == [
            {"name": "http", "port": 8000, "targetPort": "http"}
        ]
        assert "nodePort" not in str(service)
        assert "hostPort" not in str(deployment)
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["strategy"]["type"] == "Recreate"
        assert len(pod_spec["containers"]) == 1
        assert container["resources"]["requests"]["nvidia.com/gpu.shared"] == 1
        assert container["resources"]["limits"]["nvidia.com/gpu.shared"] == 1
        assert "nvidia.com/gpu" not in container["resources"]["requests"]
        assert container["image"] == "vllm/vllm-openai:v0.29.0"
        assert container["startupProbe"]["failureThreshold"] >= 120
        assert container["readinessProbe"]["httpGet"]["path"] == "/health"
        assert container["livenessProbe"]["httpGet"]["path"] == "/health"
        environment = {entry["name"]: entry for entry in container["env"]}
        assert environment["VLLM_USE_V2_MODEL_RUNNER"]["value"] == "0"

    assert len([d for d in documents if d["kind"] == "Deployment"]) == 3
    assert len([d for d in documents if d["kind"] == "Service"]) == 3
    assert len([d for d in documents if d["kind"] == "PersistentVolumeClaim"]) == 3

    # --gpu-memory-utilization is a hard pre-allocation, not a ceiling vLLM
    # grows into. These three fractions are what stops the engines colliding:
    # time-slicing hands out scheduling slots, not memory, so an overrun is an
    # OOM rather than throttling.
    utilization = [
        float(config["QWEN_AGENT_GPU_MEMORY_UTILIZATION"]),
        float(config["QWEN_SPECIALIST_GPU_MEMORY_UTILIZATION"]),
        float(config["EMBEDDING_GPU_MEMORY_UTILIZATION"]),
    ]
    assert_safe_gpu_budget(utilization)
    assert sum(utilization) == pytest.approx(0.65)

    expected_models = {
        "QWEN_AGENT_MODEL_ID": "nvidia/Qwen3-14B-NVFP4",
        "QWEN_SPECIALIST_MODEL_ID": "Qwen/Qwen2.5-1.5B-Instruct-AWQ",
        "EMBEDDING_MODEL_ID": "Qwen/Qwen3-Embedding-0.6B",
    }
    assert {key: config[key] for key in expected_models} == expected_models
    assert config["QWEN_AGENT_QUANTIZATION"] == "modelopt_fp4"
    assert config["QWEN_SPECIALIST_QUANTIZATION"] == "awq"
    # The embedding model is small enough to serve unquantized, so it has no
    # QUANTIZATION key and its Deployment passes no --quantization flag.
    assert "EMBEDDING_QUANTIZATION" not in config


def test_vllm_deployments_have_role_appropriate_tool_parsers() -> None:
    documents = load_documents("vllm.yaml")
    arguments = {
        name: resource(documents, "Deployment", name)["spec"]["template"]["spec"][
            "containers"
        ][0]["args"]
        for name in MODEL_NAMES
    }

    for name in GENERATIVE_NAMES:
        args = arguments[name]
        assert "--enable-auto-tool-choice" in args
        parser_index = args.index("--tool-call-parser")
        assert args[parser_index + 1] == "hermes"
        assert "--quantization" in args

    # The embedding engine generates nothing, so tool calling is meaningless
    # for it. It runs as a pooling model instead.
    embedding = arguments["vllm-embedding"]
    assert "--enable-auto-tool-choice" not in embedding
    assert "--tool-call-parser" not in embedding
    assert "--quantization" not in embedding
    assert embedding[embedding.index("--runner") + 1] == "pooling"

    assert all("--enforce-eager" in args for args in arguments.values())

    # --max-num-seqs is the other half of the memory decision: sizing the KV
    # cache against the card while capping concurrency at 4 reserves capacity
    # the engine can never reach.
    for name in GENERATIVE_NAMES:
        args = arguments[name]
        assert args[args.index("--max-num-seqs") + 1] == "4"


def test_nvidia_device_plugin_exposes_enough_shared_gpu_slots() -> None:
    documents = load_documents("nvidia-device-plugin-timeslicing.yaml")
    config_map = resource(
        documents, "ConfigMap", "nvidia-device-plugin-config"
    )
    config = yaml.safe_load(config_map["data"]["time-slicing"])
    sharing = config["sharing"]["timeSlicing"]

    assert config["version"] == "v1"
    assert config["flags"]["migStrategy"] == "none"
    assert sharing["renameByDefault"] is True
    assert sharing["failRequestsGreaterThanOne"] is True
    # Replicas are scheduling slots, not memory. This is a separate ceiling
    # from --gpu-memory-utilization and fails with a completely different
    # error: a pod stuck Pending on "Insufficient nvidia.com/gpu.shared"
    # rather than an OOM. It must cover every engine.
    slots = sharing["resources"][0]
    assert slots["name"] == "nvidia.com/gpu"
    assert slots["replicas"] >= len(MODEL_NAMES)


def test_gpu_budget_validation_rejects_an_overcommitted_failure_case() -> None:
    with pytest.raises(AssertionError, match="reserve 1.05"):
        assert_safe_gpu_budget([0.55, 0.30, 0.20])


def test_litellm_maps_logical_roles_to_private_backends() -> None:
    documents = load_documents("litellm.yaml")
    config_map = resource(documents, "ConfigMap", "litellm-config")
    config = yaml.safe_load(config_map["data"]["config.yaml"])
    service = resource(documents, "Service", "litellm")
    deployment = resource(documents, "Deployment", "litellm")

    # Hosted aliases carry no api_base -- the provider supplies the endpoint.
    mappings = {
        entry["model_name"]: (
            entry["litellm_params"]["model"],
            entry["litellm_params"].get("api_base"),
        )
        for entry in config["model_list"]
    }
    local = {name: value for name, value in mappings.items()
             if not name.endswith("-bedrock")}
    assert local == {
        "supervisor-model": (
            "openai/qwen-agent",
            "http://vllm-qwen-agent:8000/v1",
        ),
        "order-actions-model": (
            "openai/qwen-agent",
            "http://vllm-qwen-agent:8000/v1",
        ),
        "order-tracking-model": (
            "openai/qwen-specialist",
            "http://vllm-qwen-specialist:8000/v1",
        ),
        "infrastructure-model": (
            "openai/qwen-specialist",
            "http://vllm-qwen-specialist:8000/v1",
        ),
        "nemo-safety-model": (
            "openai/qwen-agent",
            "http://vllm-qwen-agent:8000/v1",
        ),
        "embedding-model": (
            "openai/qwen-embedding",
            "http://vllm-embedding:8000/v1",
        ),
    }

    # Every local role has a hosted counterpart under the same name plus
    # -bedrock. Switching a role from local to hosted is a config change, not
    # a code change -- and the application's own key cannot call the hosted
    # aliases, so a misconfigured role gets a 403 rather than a bill.
    hosted = {name for name in mappings if name.endswith("-bedrock")}
    assert hosted == {f"{name}-bedrock" for name in local}
    assert all(
        mappings[name][0].startswith("bedrock/") for name in hosted
    )

    assert config["general_settings"]["master_key"] == (
        "os.environ/LITELLM_MASTER_KEY"
    )
    # Only the local backends carry the shared vLLM key; the hosted aliases
    # authenticate through the provider instead.
    assert all(
        entry["litellm_params"]["api_key"] == "os.environ/VLLM_API_KEY"
        for entry in config["model_list"]
        if not entry["model_name"].endswith("-bedrock")
    )

    # Spend attribution and the Prometheus scrape are the same wiring: both
    # are properties of this hop, and only work because nothing routes around
    # it.
    assert "prometheus" in config["litellm_settings"]["callbacks"]
    assert service["spec"]["type"] == "ClusterIP"
    assert "nodePort" not in str(service)
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "ghcr.io/berriai/litellm:v1.101.0"
    assert "nvidia.com/gpu" not in str(deployment)


def test_litellm_uses_the_shared_postgres_rather_than_its_own() -> None:
    """LiteLLM once ran a private StatefulSet. Three Postgres instances for one
    deployment is three sets of backups, upgrades and passwords, so they were folded
    into one server holding a database per service. This asserts the
    consolidation, because the failure mode of regressing it is silent: a
    second database appears and works fine until someone looks for the data.
    """
    documents = load_documents("litellm.yaml")

    assert not [d for d in documents if d["kind"] == "StatefulSet"], (
        "LiteLLM should not own a database; k8s/postgres.yaml is the only one"
    )

    deployment = resource(documents, "Deployment", "litellm")
    proxy_container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {entry["name"]: entry for entry in proxy_container["env"]}

    assert environment["UI_USERNAME"]["value"] == "admin"
    assert environment["UI_PASSWORD"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-secrets",
        "key": "ui-password",
    }
    assert environment["DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "litellm-db-secret",
        "key": "database-url",
    }


def test_one_postgres_holds_a_database_per_service() -> None:
    documents = load_documents("postgres.yaml")
    statefulsets = [d for d in documents if d["kind"] == "StatefulSet"]
    assert [s["metadata"]["name"] for s in statefulsets] == ["postgres-db"]

    initdb = resource(documents, "ConfigMap", "postgres-initdb")
    sql = "\n".join(initdb["data"].values())
    for database in (
        "temporal",
        "temporal_visibility",
        "litellm",
        "keycloak",
        "agentlab",
    ):
        assert f"CREATE DATABASE {database}" in sql

    # Conversation summaries are searched by meaning, which needs the
    # extension present before the application creates its HNSW index.
    assert "CREATE EXTENSION" in sql and "vector" in sql


def test_envoy_gateway_exposes_hostname_bound_litellm_ui_and_api() -> None:
    documents = load_documents("envoy-gateway.yaml")
    controller_values = yaml.safe_load(
        (ROOT / "k8s" / "envoy-gateway-values.yaml").read_text(encoding="utf-8")
    )
    proxy = resource(documents, "EnvoyProxy", "agentops-inference-proxy")
    gateway_class = resource(documents, "GatewayClass", "agentops-envoy")
    gateway = resource(documents, "Gateway", "inference-gateway")
    route = resource(documents, "HTTPRoute", "litellm-inference")
    ui_route = resource(documents, "HTTPRoute", "litellm-ui")

    provider = proxy["spec"]["provider"]["kubernetes"]
    assert provider["envoyService"]["name"] == "agentops-inference-gateway"
    assert provider["envoyService"]["type"] == "LoadBalancer"
    assert controller_values["service"]["type"] == "ClusterIP"
    assert controller_values["deployment"]["replicas"] == 1
    assert gateway_class["spec"]["controllerName"] == (
        "gateway.envoyproxy.io/gatewayclass-controller"
    )

    listener = gateway["spec"]["listeners"][0]
    assert listener == {
        "name": "litellm-http",
        "hostname": "litellm.agentops.local",
        "protocol": "HTTP",
        "port": 80,
        "allowedRoutes": {"namespaces": {"from": "Same"}},
    }
    assert gateway["spec"]["infrastructure"]["parametersRef"] == {
        "group": "gateway.envoyproxy.io",
        "kind": "EnvoyProxy",
        "name": "agentops-inference-proxy",
    }

    assert route["spec"]["parentRefs"] == [
        {"name": "inference-gateway", "sectionName": "litellm-http"}
    ]
    assert route["spec"]["hostnames"] == ["litellm.agentops.local"]
    rules = route["spec"]["rules"]
    assert [rule["matches"][0]["path"] for rule in rules] == [
        {"type": "PathPrefix", "value": "/v1"},
        {"type": "Exact", "value": "/health"},
    ]
    assert all(
        rule["backendRefs"] == [{"name": "litellm", "port": 4000}]
        for rule in rules
    )
    assert rules[0]["timeouts"]["request"] == "0s"
    assert ui_route["spec"]["parentRefs"] == [
        {"name": "inference-gateway", "sectionName": "litellm-http"}
    ]
    assert ui_route["spec"]["hostnames"] == ["litellm.agentops.local"]
    assert ui_route["spec"]["rules"] == [
        {
            "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
            "timeouts": {"request": "0s"},
            "backendRefs": [{"name": "litellm", "port": 4000}],
        }
    ]
    assert "NodePort" not in str(proxy)


def test_litellm_network_policy_allows_only_envoy_gateway_namespace() -> None:
    documents = load_documents("envoy-gateway.yaml")
    policy = resource(
        documents, "NetworkPolicy", "litellm-ingress-via-envoy-only"
    )

    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "litellm"}}
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    assert policy["spec"]["ingress"] == [
        {
            "from": [
                {
                    "namespaceSelector": {
                        "matchLabels": {
                            "kubernetes.io/metadata.name": "envoy-gateway-system"
                        }
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 4000}],
        }
    ]
