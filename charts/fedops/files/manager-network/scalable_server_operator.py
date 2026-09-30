from utils.deployment_config import TARGET_NAMESPACE, VIRTUAL_SERVICE_NAME, ISTIO_GATEWAY, ISTIO_HOST, TASK_IMAGE, TASK_S3_SECRET_NAME, task_connection_env, required_url
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import logging
import os
import json
import time

from typing import Optional, Dict, Any, List
from kubernetes.client import V1DeleteOptions
import contextlib
import threading

from utils.network_config import FL_SERVER_PORT_MIN, iter_fl_server_ports
from utils.resource_names import fl_server_service_name
from utils.runtime_bootstrap import (
    classic_runtime_bootstrap_shell,
    classic_runtime_readiness_shell,
)
from utils.runtime_profiles import resolve_runtime_profile
from utils.storage_config import hostpath_storage_node, task_storage_path, task_storage_capacity


_PORT_ALLOCATION_LOCK = threading.Lock()
EXTERNAL_IP_TIMEOUT_SECONDS = 180.0


def _create_deployment_after_prior_deletion(
    apps_v1,
    namespace: str,
    deployment_name: str,
    deployment,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.5,
) -> bool:
    """Create a Deployment, waiting for an earlier deletion to finish.

    Kubernetes can return HTTP 409 with ``object is being deleted`` when a user
    quickly recreates a runtime. Treating every 409 as an existing healthy
    Deployment leaves a Service without any Pod. A non-terminating Deployment
    remains an idempotent success; a terminating one is retried until gone.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            apps_v1.create_namespaced_deployment(namespace, deployment)
            return True
        except ApiException as exc:
            if exc.status != 409:
                raise

            try:
                existing = apps_v1.read_namespaced_deployment(
                    name=deployment_name,
                    namespace=namespace,
                )
            except ApiException as read_exc:
                if read_exc.status == 404 and time.monotonic() < deadline:
                    time.sleep(poll_seconds)
                    continue
                raise

            deletion_timestamp = getattr(
                getattr(existing, "metadata", None), "deletion_timestamp", None
            )
            if deletion_timestamp is None:
                return False
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for Deployment {deployment_name} deletion"
                )
            logging.info(
                "Deployment %s is still terminating; waiting before recreation",
                deployment_name,
            )
            time.sleep(poll_seconds)


def _task_pod_scheduling():
    selector = json.loads(os.getenv("FEDOPS_TASK_NODE_SELECTOR", "{}"))
    if not isinstance(selector, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in selector.items()):
        raise ValueError("FEDOPS_TASK_NODE_SELECTOR must be a string map")
    return {
        "node_selector": selector,
        "topology_spread_constraints": (
            [client.V1TopologySpreadConstraint(
                max_skew=1, topology_key="kubernetes.io/hostname",
                when_unsatisfiable="ScheduleAnyway",
                label_selector=client.V1LabelSelector(match_labels={"app": "fl-server"}),
            )] if os.getenv("FEDOPS_DEPLOYMENT_TOPOLOGY") == "multi-node" else None
        ),
    }


def _hostpath_node_affinity(node_name: str):
    return client.V1VolumeNodeAffinity(
        required=client.V1NodeSelector(
            node_selector_terms=[
                client.V1NodeSelectorTerm(
                    match_expressions=[
                        client.V1NodeSelectorRequirement(
                            key="kubernetes.io/hostname",
                            operator="In",
                            values=[node_name],
                        )
                    ]
                )
            ]
        )
    )


def create_scalable_fl_server(task_id: str, fl_server_status: dict, server_repo_addr,
                             initial_cpu="1", initial_memory="2Gi", namespace=TARGET_NAMESPACE, task_data=None,
                             runtime_contract=None):
    """스케일링 가능한 FL 서버 생성 (Deployment + PVC)"""
    load_config()

    # Status 즉시 초기화 (기존 FL server와 동일)
    fl_server_status[task_id] = {"status": "FL Server Creating"}

    logging.info(f"Creating scalable FL server for task: {task_id}")
    logging.info(f"Server repo address: {server_repo_addr}")
    runtime_release = getattr(task_data, "runtime_release", None) if task_data else None
    profile = resolve_runtime_profile(
        getattr(task_data, "runtime_contract", None) or runtime_contract,
        getattr(runtime_release, "source_revision", None),
    )
    logging.info(
        "Runtime contract: %s (FedOps %s, source %s)",
        profile.name,
        profile.fedops_version,
        profile.source_revision,
    )
    runtime_release = getattr(task_data, "runtime_release", None) if task_data else None
    if profile.name == "federated-task-v3":
        if runtime_release is None:
            raise ValueError("federated-task-v3 requires an exact Runtime Release descriptor")
        if (
            runtime_release.fedops_version != profile.fedops_version
            or runtime_release.source_revision != profile.source_revision
        ):
            raise ValueError("Runtime Release does not match the selected immutable profile")
    elif runtime_release is not None:
        raise ValueError("Runtime Release descriptors are only accepted by federated-task-v3")

    # Log FL configuration if provided
    if task_data:
        logging.info(f"FL Configuration received:")
        logging.info(f"  - Data type: {task_data.data_type}")
        logging.info(f"  - Model type: {task_data.model_type}")
        logging.info(f"  - Strategy: {task_data.strategy}")
        logging.info(f"  - Learning rate: {task_data.learning_rate}")
        logging.info(f"  - Epochs: {task_data.num_epochs}")
        logging.info(f"  - Batch size: {task_data.batch_size}")
        logging.info(f"  - Rounds: {task_data.num_rounds}")
        logging.info(f"  - Clients per round: {task_data.client_per_round}")
        if task_data.yaml_config:
            logging.info(f"  - YAML config provided: {len(task_data.yaml_config)} characters")

    # 1. PVC 생성 (데이터 영속성)
    pvc_name = create_persistent_volume_and_claim(task_id, namespace)

    # 2. Deployment 생성
    deployment_name = f"fl-server-deploy-{task_id}"
    service_name = fl_server_service_name(task_id)

    if server_repo_addr == '':
        server_repo_addr = 'https://github.com/gachon-CCLab/FedOps-Training-Server.git'

    env_vars = [
        client.V1EnvVar(name="REPO_URL", value=server_repo_addr),
        client.V1EnvVar(name="GIT_TAG", value="main"),
        client.V1EnvVar(name="ENV", value="init"),
        client.V1EnvVar(name="TASK_ID", value=task_id),
        client.V1EnvVar(name="FEDOPS_RUNTIME_CONTRACT", value=profile.name),
        client.V1EnvVar(name="FEDOPS_PACKAGE_VERSION", value=profile.fedops_version),
        client.V1EnvVar(name="FEDOPS_SOURCE_REVISION", value=profile.source_revision),
        # Server Manager 연결 정보
        client.V1EnvVar(name="FL_SERVER_MODE", value="scalable"),
        # S3 secrets
        client.V1EnvVar(name="ACCESS_KEY_ID",
                       value_from=client.V1EnvVarSource(
                           secret_key_ref=client.V1SecretKeySelector(name=TASK_S3_SECRET_NAME, key='ACCESS_KEY_ID'))),
        client.V1EnvVar(name="ACCESS_SECRET_KEY",
                       value_from=client.V1EnvVarSource(
                           secret_key_ref=client.V1SecretKeySelector(name=TASK_S3_SECRET_NAME, key='ACCESS_SECRET_KEY'))),
        client.V1EnvVar(name="BUCKET_NAME",
                       value_from=client.V1EnvVarSource(
                           secret_key_ref=client.V1SecretKeySelector(name=TASK_S3_SECRET_NAME, key='BUCKET_NAME')))
    ]
    env_vars.extend(client.V1EnvVar(name=key, value=value) for key, value in task_connection_env().items())
    if runtime_release is not None:
        env_vars.extend([
            client.V1EnvVar(name="FEDOPS_SERVER_VALIDATION_ROOT", value="/app/data/server-validation"),
            client.V1EnvVar(name="FEDOPS_RELEASE_ID", value=runtime_release.release_id),
            client.V1EnvVar(name="FEDOPS_RELEASE_ARCHIVE_URL", value=runtime_release.archive_url),
            client.V1EnvVar(name="FEDOPS_RELEASE_ARCHIVE_SHA256", value=runtime_release.archive_sha256),
            client.V1EnvVar(name="FEDOPS_INITIAL_MODEL_URL", value=runtime_release.model_url),
            client.V1EnvVar(name="FEDOPS_INITIAL_MODEL_SHA256", value=runtime_release.model_sha256),
            client.V1EnvVar(name="FEDOPS_INITIAL_MODEL_FORMAT", value=runtime_release.model_format),
        ])
    campaign_config = getattr(task_data, "campaign_config", None) if task_data else None
    if campaign_config is not None:
        campaign_payload = (
            campaign_config.model_dump()
            if hasattr(campaign_config, "model_dump")
            else campaign_config.dict()
        )
        env_vars.append(client.V1EnvVar(
            name="FEDOPS_CAMPAIGN_CONFIG",
            value=json.dumps(campaign_payload, separators=(",", ":")),
        ))

    # FL 설정 환경변수 추가
    if task_data:
        # model_type 변환: AI -> Pytorch, LLM -> Huggingface
        converted_model_type = ""
        if task_data.model_type:
            if task_data.model_type.upper() == "AI":
                converted_model_type = "Pytorch"
            elif task_data.model_type.upper() == "LLM":
                converted_model_type = "Huggingface"
            else:
                converted_model_type = task_data.model_type  # 기존 값 유지

        sba_fl_target = getattr(task_data, "sba_fl_target", None) or ""
        if not sba_fl_target and isinstance(task_data.dataset_params, dict):
            sba_fl_target = task_data.dataset_params.get("sbaFlTarget", "")

        fl_env_vars = [
            client.V1EnvVar(name="FL_DATA_TYPE", value=task_data.data_type or ""),
            client.V1EnvVar(name="FL_MODEL_TYPE", value=converted_model_type),
            client.V1EnvVar(name="FL_LEARNING_RATE", value=task_data.learning_rate or ""),
            client.V1EnvVar(name="FL_NUM_EPOCHS", value=task_data.num_epochs or ""),
            client.V1EnvVar(name="FL_BATCH_SIZE", value=task_data.batch_size or ""),
            client.V1EnvVar(name="FL_NUM_ROUNDS", value=task_data.num_rounds or ""),
            client.V1EnvVar(name="FL_CLIENT_PER_ROUND", value=task_data.client_per_round or ""),
            client.V1EnvVar(name="FL_STRATEGY", value=task_data.strategy or ""),
            client.V1EnvVar(name="FL_XAI_ENABLED", value=task_data.xai_enabled or ""),
            client.V1EnvVar(name="FL_SBA_TARGET", value=sba_fl_target),
        ]

        logging.info(f"Model type conversion: {task_data.model_type} -> {converted_model_type}")

        # Add strategy parameters as JSON string
        if task_data.strategy_params:
            fl_env_vars.append(client.V1EnvVar(name="FL_STRATEGY_PARAMS", value=json.dumps(task_data.strategy_params)))

        # Add LLM parameters as JSON string
        if task_data.llm_params:
            fl_env_vars.append(client.V1EnvVar(name="FL_LLM_PARAMS", value=json.dumps(task_data.llm_params)))

        # Add dataset parameters as JSON string
        if task_data.dataset_params:
            fl_env_vars.append(client.V1EnvVar(name="FL_DATASET_PARAMS", value=json.dumps(task_data.dataset_params)))

        # Add YAML config as environment variable
        if task_data.yaml_config:
            fl_env_vars.append(client.V1EnvVar(name="FL_YAML_CONFIG", value=task_data.yaml_config))
            fl_env_vars.append(client.V1EnvVar(name="FL_YAML_CONFIG_PATH", value="/app/conf/config.yaml"))

        env_vars.extend(fl_env_vars)
        logging.info(f"Added {len(fl_env_vars)} FL environment variables")

    # 볼륨 마운트 설정
    volume_mounts = [
        client.V1VolumeMount(
            name="fl-data",
            mount_path="/app/data"  # 데이터 저장 경로
        )
    ]

    volumes = [
        client.V1Volume(
            name="fl-data",
            persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                claim_name=pvc_name
            )
        )
    ]

    deployment = client.V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=client.V1ObjectMeta(
            name=deployment_name,
            namespace=namespace,
            labels={"app": "fl-server", "task_id": task_id}
        ),
        spec=client.V1DeploymentSpec(
            replicas=1,
            selector=client.V1LabelSelector(
                match_labels={"app": "fl-server", "task_id": task_id}
            ),
            template=client.V1PodTemplateSpec(
                metadata=client.V1ObjectMeta(
                    labels={"app": "fl-server", "task_id": task_id},
                    annotations=(
                        {
                            "fedops.io/release-id": runtime_release.release_id,
                            "fedops.io/release-sha256": runtime_release.archive_sha256,
                        }
                        if runtime_release is not None else None
                    ),
                ),
                spec=client.V1PodSpec(
                    **_task_pod_scheduling(),
                    containers=[
                        client.V1Container(
                            name="fl-server",
                            image=TASK_IMAGE,
                            ports=[
                                client.V1ContainerPort(container_port=8080)
                            ],
                            command=["/bin/sh", "-c"],
                            args=[
                                # ====== 진단 출력 ======
                                "set -e; echo '=== Starting FL Server Setup ===' && "
                                "echo 'Current directory:' && pwd && "
                                "echo 'Environment variables:' && env | grep FL_ && "
                                "echo 'FL_MODEL_TYPE: '$FL_MODEL_TYPE && "
                                "echo 'FEDOPS_RUNTIME_CONTRACT: '$FEDOPS_RUNTIME_CONTRACT && "
                                "echo 'FEDOPS_SOURCE_REVISION: '$FEDOPS_SOURCE_REVISION && "

                                # v3 executes the exact immutable Release submitted by Agent Studio.
                                "if [ \"$FEDOPS_RUNTIME_CONTRACT\" = \"federated-task-v3\" ]; then ("
                                    "echo 'Downloading exact Federated Task Release...' && "
                                    "rm -rf /app/code /tmp/fedops-release.zip /tmp/fedops-initial-model && "
                                    "rm -f /app/data/release.identity && "
                                    "mkdir -p /app/code /app/data/logs /app/data/models /app/data/checkpoints && "
                                    "python3 -c 'import hashlib,os,urllib.request,zipfile; "
                                    "a=\"/tmp/fedops-release.zip\"; m=\"/tmp/fedops-initial-model\"; "
                                    "urllib.request.urlretrieve(os.environ[\"FEDOPS_RELEASE_ARCHIVE_URL\"],a); "
                                    "urllib.request.urlretrieve(os.environ[\"FEDOPS_INITIAL_MODEL_URL\"],m); "
                                    "h=lambda p: hashlib.sha256(open(p,\"rb\").read()).hexdigest(); "
                                    "assert h(a)==os.environ[\"FEDOPS_RELEASE_ARCHIVE_SHA256\"],\"Release checksum mismatch\"; "
                                    "assert h(m)==os.environ[\"FEDOPS_INITIAL_MODEL_SHA256\"],\"Initial Model checksum mismatch\"; "
                                    "z=zipfile.ZipFile(a); root=os.path.realpath(\"/app/code\"); "
                                    "assert all(os.path.commonpath([root,os.path.realpath(os.path.join(root,n))])==root for n in z.namelist()),\"Unsafe Release path\"; "
                                    "z.extractall(root)' && "
                                    "mkdir -p /app/code/model_release && "
                                    "cp /tmp/fedops-initial-model /app/code/model_release/model.safetensors && "
                                    "cd /app/code && "
                                    "python3 -m pip install 'uv==0.8.13' && "
                                    "uv sync --frozen --link-mode copy && "
                                    ".venv/bin/python -c 'import torch,torchvision; "
                                    "assert torch.version.cuda is None, "
                                    "f\"FedOps 1.3 aggregation requires the CPU Torch Release contract, got CUDA {torch.version.cuda}\"; "
                                    "assert torchvision.extension._has_ops(), "
                                    "\"FedOps 1.3 aggregation requires a matching CPU TorchVision wheel\"' && "
                                    "printf '%s  %s\\n' \"$FEDOPS_RELEASE_ARCHIVE_SHA256\" \"$FEDOPS_RELEASE_ID\" > /app/data/release.identity && "
                                    "mkdir -p /app/data/server-validation && "
                                    "echo 'Exact Federated Task Release is ready' && "
                                    "(curl -X PUT \"${SERVER_MANAGER_URL}/FLSe/ScalableServerReady/${TASK_ID}\" -H 'Content-Type: application/json' || "
                                    "echo 'WARNING: Runtime is ready but the Server Manager callback failed') && "
                                    "exec tail -f /dev/null"
                                    ") || { rc=$?; "
                                    "echo \"ERROR: FedOps 1.3 Runtime Release bootstrap failed (exit $rc).\" >&2; "
                                    "exit \"$rc\"; }; "
                                "fi; "

                                # ====== 코드 준비: FL_MODEL_TYPE에 따라 다른 코드 준비 ======
                                "if [ \"$FL_MODEL_TYPE\" = \"Pytorch\" ]; then "
                                    "echo 'Setting up Pytorch (AI) FL code...' && "
                                    # Pytorch/AI용 MNIST 코드 준비
                                    "git clone https://github.com/gachon-CCLab/FedOps.git FedOps && "
                                    "cd FedOps && git checkout --detach \"$FEDOPS_SOURCE_REVISION\" && cd .. && "
                                    "mv FedOps/silo/examples/torch/MNIST /tmp/MNIST && "
                                    "rm -rf FedOps && "
                                    "mkdir -p /app/code && "
                                    "cp -r /tmp/MNIST/* /app/code/ 2>/dev/null || true && "
                                    "(cp -r /tmp/MNIST/.[!.]* /app/code/ 2>/dev/null || true) && "
                                    "rm -rf /tmp/MNIST && "
                                    "echo 'Pytorch FL code setup complete' && "
                                    "ls -la /app/code; "
                                "elif [ \"$FL_MODEL_TYPE\" = \"Huggingface\" ]; then "
                                    "echo 'Setting up Huggingface (LLM) FL code...' && "
                                    # Huggingface/LLM용 코드 준비
                                    "git clone https://github.com/gachon-CCLab/FedOps.git FedOps && "
                                    "cd FedOps && git checkout --detach \"$FEDOPS_SOURCE_REVISION\" && cd .. && "
                                    "mkdir -p /app/code && "
                                    "cp -r FedOps/llm/usecase/finetune/* /app/code/ 2>/dev/null || true && "
                                    "(cp -r FedOps/llm/usecase/finetune/.[!.]* /app/code/ 2>/dev/null || true) && "
                                    "rm -rf FedOps && "
                                    "echo 'Huggingface FL code setup complete' && "
                                    "ls -la /app/code; "
                                "elif [ \"$FL_MODEL_TYPE\" = \"SBA-FL\" ]; then "
                                    "echo 'Setting up SBA-FL on-device FL code...' && "
                                    "echo 'FL_SBA_TARGET: '$FL_SBA_TARGET && "
                                    "git clone https://github.com/gachon-CCLab/FedOps.git FedOps && "
                                    "cd FedOps && git checkout --detach \"$FEDOPS_SOURCE_REVISION\" && cd .. && "
                                    "mkdir -p /app/code && "
                                    "cp -r FedOps/mobile/examples/server/* /app/code/ 2>/dev/null || true && "
                                    "(cp -r FedOps/mobile/examples/server/.[!.]* /app/code/ 2>/dev/null || true) && "
                                    "rm -rf FedOps && "
                                    "echo 'SBA-FL code setup complete' && "
                                    "ls -la /app/code; "
                                "else "
                                    "echo 'Unknown FL_MODEL_TYPE: '$FL_MODEL_TYPE && "
                                    "echo 'Using default Pytorch setup...' && "
                                    # 기본값으로 Pytorch 코드 사용
                                    "git clone https://github.com/gachon-CCLab/FedOps.git FedOps && "
                                    "cd FedOps && git checkout --detach \"$FEDOPS_SOURCE_REVISION\" && cd .. && "
                                    "mv FedOps/silo/examples/torch/MNIST /tmp/MNIST && "
                                    "rm -rf FedOps && "
                                    "mkdir -p /app/code && "
                                    "cp -r /tmp/MNIST/* /app/code/ 2>/dev/null || true && "
                                    "(cp -r /tmp/MNIST/.[!.]* /app/code/ 2>/dev/null || true) && "
                                    "rm -rf /tmp/MNIST && "
                                    "echo 'Default Pytorch FL code setup complete' && "
                                    "ls -la /app/code; "
                                "fi; "

                                # ====== YAML 설정 파일 생성 ======
                                "if [ ! -z \"$FL_YAML_CONFIG\" ]; then "
                                    "echo 'Creating config directory and file...' && "
                                    "mkdir -p /app/code/conf && "
                                    "echo \"$FL_YAML_CONFIG\" > /app/code/conf/config.yaml && "
                                    "echo 'Config file created' && "
                                    "ls -la /app/code/conf/; "
                                "fi; "

                                # ====== 데이터 디렉토리 생성(그대로 유지) ======
                                "mkdir -p /app/data/logs /app/data/models /app/data/checkpoints && "
                                "echo 'Directories created successfully' && "

                                # ====== PVC에 보존되는 classic Runtime ======
                                + classic_runtime_bootstrap_shell()

                                # ====== FL 서버 준비 완료 상태 업데이트 ======
                                + "echo 'FL server setup completed. Updating status...' && "
                                "curl -X PUT \"${SERVER_MANAGER_URL}/FLSe/ScalableServerReady/${TASK_ID}\" "
                                "-H 'Content-Type: application/json' || "
                                "echo 'WARNING: Failed to update server status to FL Server created' && "

                                # ====== 서버 실행(항상 keep-alive) ======
                                "echo '[FL server Pod ready - Waiting for server_main.py execution]' && "
                                "exec tail -f /dev/null"
                            ],
                            env=env_vars,
                            volume_mounts=volume_mounts,
                            readiness_probe=client.V1Probe(
                                _exec=client.V1ExecAction(
                                    command=[
                                        "/bin/sh",
                                        "-c",
                                        (
                                            "test -s /app/data/release.identity && "
                                            "test -x /app/code/.venv/bin/python && "
                                            "/app/code/.venv/bin/python -c \"import fedops, federated_task, torch, torchvision; "
                                            "assert torch.version.cuda is None; "
                                            "assert torchvision.extension._has_ops()\""
                                            if profile.name == "federated-task-v3"
                                            else classic_runtime_readiness_shell()
                                        ),
                                    ]
                                ),
                                initial_delay_seconds=2,
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=3,
                            ),
                            resources=client.V1ResourceRequirements(
                                requests={"cpu": initial_cpu, "memory": initial_memory},
                                limits={"cpu": initial_cpu, "memory": initial_memory}
                            )
                        )
                    ],
                    volumes=volumes,
                    restart_policy="Always"
                )
            )
        )
    )

    # Deployment 생성
    apps_v1 = client.AppsV1Api()
    try:
        created = _create_deployment_after_prior_deletion(
            apps_v1,
            namespace,
            deployment_name,
            deployment,
        )
        if not created:
            logging.info(f"Deployment {deployment_name} already exists")
        else:
            logging.info(f"Created deployment: {deployment_name}")
    except (ApiException, TimeoutError):
        fl_server_status[task_id] = {
            "status": "FL Server Error",
            "message": "The previous runtime is still terminating. Retry creation shortly.",
        }
        raise
    # Update the status in the shared dictionary
    # Deployment 생성 직후 - Pod 내부에서 준비 완료 시 "FL Server created"로 업데이트됨
    # fl_server_status[task_id]["status"] = "FL Server Creating"

    # Publish resources as soon as they exist so Web can distinguish an active
    # allocation from a runtime that was never created. Network fields follow
    # after MetalLB and the Task ID route are ready.
    service_name = fl_server_service_name(task_id)
    fl_server_status[task_id].update({
        "deployment": deployment_name,
        "pvc": pvc_name,
        "cpu": initial_cpu,
        "memory": initial_memory,
        "service_name": service_name,
        "release_id": runtime_release.release_id if runtime_release is not None else None,
        "release_sha256": runtime_release.archive_sha256 if runtime_release is not None else None,
        "campaign": campaign_payload if campaign_config is not None else None,
    })

    try:
        # Service creation is part of the reported network allocation. Keeping
        # it in this error boundary preserves the already-created Deployment,
        # PVC, CPU, and memory details when Kubernetes rejects the Service.
        create_service_for_deployment(task_id, namespace)

        # 외부 IP 할당 대기
        external_ip = wait_for_external_ip(service_name, namespace)

        # 포트 할당 및 VirtualService 업데이트
        # Task ID remains the client connection identity. Allocate and publish its
        # gateway route as one idempotent operation so retries reuse the same port.
        with _PORT_ALLOCATION_LOCK:
            requested_port = get_unused_port(service_name, namespace)
            port = update_virtual_service(
                task_id, service_name, requested_port, namespace, external_ip
            )
    except Exception as error:
        logging.exception("Failed to allocate the network route for task %s", task_id)
        fl_server_status[task_id].update({
            "status": "FL Server Error",
            "message": str(error),
        })
        raise

    fl_server_status[task_id].update({
        "port": port,
        "external_ip": external_ip,
    })

    logging.info(f"Scalable FL server created successfully for task: {task_id}")
    logging.info(f"Connection info - IP: {external_ip}, Port: {port}")

    return deployment_name

def scale_fl_server_resources(task_id: str, cpu: str, memory: str, namespace=TARGET_NAMESPACE):
    """FL 서버의 리소스를 동적으로 스케일링"""
    load_config()

    deployment_name = f"fl-server-deploy-{task_id}"

    apps_v1 = client.AppsV1Api()

    try:
        # 현재 Deployment 가져오기
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)

        # 리소스 업데이트
        deployment.spec.template.spec.containers[0].resources = client.V1ResourceRequirements(
            requests={"cpu": cpu, "memory": memory},
            limits={"cpu": cpu, "memory": memory}
        )

        # Deployment 업데이트
        apps_v1.patch_namespaced_deployment(deployment_name, namespace, deployment)
        logging.info(f"Scaled {deployment_name} to CPU: {cpu}, Memory: {memory}")

        return True
    except client.exceptions.ApiException as e:
        logging.error(f"Failed to scale deployment: {e}")
        return False

def pause_fl_server(task_id: str, namespace=TARGET_NAMESPACE):
    """FL 서버를 일시정지 (리소스 해제, 데이터 보존)"""
    load_config()

    deployment_name = f"fl-server-deploy-{task_id}"

    apps_v1 = client.AppsV1Api()

    try:
        # Deployment를 0 replica로 스케일링
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
        deployment.spec.replicas = 0

        apps_v1.patch_namespaced_deployment(deployment_name, namespace, deployment)
        logging.info(f"Paused {deployment_name} (scaled to 0 replicas)")

        return True
    except client.exceptions.ApiException as e:
        logging.error(f"Failed to pause deployment: {e}")
        return False

def resume_fl_server(task_id: str, namespace=TARGET_NAMESPACE):
    """FL 서버를 재개 (데이터 보존된 상태에서 리소스 재할당)"""
    load_config()

    deployment_name = f"fl-server-deploy-{task_id}"

    apps_v1 = client.AppsV1Api()

    try:
        # Deployment를 1 replica로 스케일링
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
        deployment.spec.replicas = 1

        apps_v1.patch_namespaced_deployment(deployment_name, namespace, deployment)
        logging.info(f"Resumed {deployment_name} (scaled to 1 replica)")

        return True
    except client.exceptions.ApiException as e:
        logging.error(f"Failed to resume deployment: {e}")
        return False

def create_service_for_deployment(task_id: str, namespace=TARGET_NAMESPACE):
    """Deployment를 위한 Service 생성"""
    service_name = fl_server_service_name(task_id)

    service = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=service_name,
            namespace=namespace,
            labels={"task_id": task_id},
        ),
        spec=client.V1ServiceSpec(
            selector={"app": "fl-server", "task_id": task_id},
            ports=[client.V1ServicePort(port=80, target_port=8080)],
            type=os.getenv("FEDOPS_TASK_SERVICE_TYPE", "LoadBalancer")
        )
    )

    core_v1 = client.CoreV1Api()
    try:
        core_v1.create_namespaced_service(namespace, service)
        logging.info(f"Created service: {service_name}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            logging.info(f"Service {service_name} already exists")
        else:
            raise e

def create_persistent_volume_and_claim(task_id: str, namespace: str = TARGET_NAMESPACE):
    """태스크별 데이터 저장을 위한 PV + PVC 생성 (storageClassName에 task_id 사용)"""
    pvc_name = f"fl-data-{task_id}"
    pv_name = f"fl-pv-{task_id}"
    storage_class_name = f"sc-{task_id}"
    storage_node = hostpath_storage_node()

    # PV 생성
    pv = client.V1PersistentVolume(
        metadata=client.V1ObjectMeta(
            name=pv_name,
            labels={"task_id": task_id, "type": "fl-data"},
            annotations={"fedops.io/storage-node": storage_node},
        ),
        spec=client.V1PersistentVolumeSpec(
            capacity={"storage": task_storage_capacity()},
            access_modes=["ReadWriteOnce"],
            persistent_volume_reclaim_policy="Retain",
            storage_class_name=storage_class_name,
            host_path=client.V1HostPathVolumeSource(path=task_storage_path(task_id)),
            node_affinity=_hostpath_node_affinity(storage_node),
        )
    )

    v1 = client.CoreV1Api()
    try:
        v1.create_persistent_volume(pv)
        logging.info(f"Created PV: {pv_name}")
    except client.exceptions.ApiException as e:
        if e.status == 409:
            logging.info(f"PV {pv_name} already exists")
        else:
            raise e

    # PVC 생성
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(
            name=pvc_name,
            namespace=namespace,
            labels={"task_id": task_id, "type": "fl-data"}
        ),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": task_storage_capacity()}
            ),
            storage_class_name=storage_class_name
        )
    )
    try:
        v1.create_namespaced_persistent_volume_claim(namespace, pvc)
        logging.info(f"Created PVC: {pvc_name}")
        return pvc_name
    except client.exceptions.ApiException as e:
        if e.status == 409:
            logging.info(f"PVC {pvc_name} already exists")
            return pvc_name
        raise e

def load_config():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config('config.txt')

def wait_for_external_ip(
    service_name: str,
    namespace: str,
    timeout_seconds: float = EXTERNAL_IP_TIMEOUT_SECONDS,
    poll_seconds: float = 5.0,
):
    """LoadBalancer 서비스의 외부 IP 할당 대기"""
    public_host = os.getenv("FEDOPS_TASK_PUBLIC_HOST", "").strip()
    if public_host:
        return public_host
    load_config()
    core_v1_api = client.CoreV1Api()
    deadline = time.monotonic() + timeout_seconds

    logging.info(f"Waiting for external IP for service: {service_name}")

    while True:
        service = core_v1_api.read_namespaced_service(namespace=namespace, name=service_name)
        if service.status.load_balancer.ingress:
            external_ip = service.status.load_balancer.ingress[0].ip
            logging.info(f"External IP assigned: {external_ip}")
            return external_ip
        else:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for external IP for Service {service_name}"
                )
            logging.info("Waiting for external IP...")
            time.sleep(poll_seconds)

def get_unused_port(service_name: str, namespace: str = TARGET_NAMESPACE):
    """사용되지 않은 포트 반환"""
    load_config()
    api_instance = client.CustomObjectsApi()

    virtual_service_name = VIRTUAL_SERVICE_NAME

    try:
        virtual_service = api_instance.get_namespaced_custom_object(
            group="networking.istio.io",
            version="v1alpha3",
            namespace=namespace,
            plural="virtualservices",
            name=virtual_service_name
        )
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # VirtualService가 없으면 최소 포트 반환
            return FL_SERVER_PORT_MIN
        else:
            raise e

    # 현재 사용 중인 포트 확인
    used_ports = []
    if "tcp" in virtual_service["spec"] and isinstance(virtual_service["spec"]["tcp"], list):
        for route in virtual_service["spec"]["tcp"]:
            if "match" in route and route["match"]:
                used_ports.append(route["match"][0]["port"])

    # 사용되지 않은 포트 반환
    for port in iter_fl_server_ports():
        if port not in used_ports:
            return port

    raise Exception("No unused ports available")

def update_virtual_service(task_id: str, service_name: str, port: int, namespace: str, external_ip: str) -> int:
    """Istio VirtualService 업데이트"""
    load_config()
    api_instance = client.CustomObjectsApi()

    virtual_service_name = VIRTUAL_SERVICE_NAME
    try:
        # 기존 VirtualService 가져오기
        virtual_service = api_instance.get_namespaced_custom_object(
            group="networking.istio.io",
            version="v1alpha3",
            namespace=namespace,
            plural="virtualservices",
            name=virtual_service_name
        )
    except client.exceptions.ApiException as e:
        if e.status == 404:
            # VirtualService가 없으면 새로 생성
            virtual_service = {
                "apiVersion": "networking.istio.io/v1alpha3",
                "kind": "VirtualService",
                "metadata": {
                    "name": virtual_service_name,
                    "namespace": namespace
                },
                "spec": {
                    "gateways": [ISTIO_GATEWAY],
                    "hosts": [ISTIO_HOST],
                    "tcp": []
                }
            }
        else:
            raise e

    # 새 라우트 추가
    new_host = f"{service_name}.{namespace}.svc.cluster.local"
    new_route = {
        "match": [{
            "port": port
        }],
        "route": [
            {
                "destination": {
                    "host": new_host,
                    "port": {"number": 80}
                }
            }
        ]
    }

    # 중복 호스트 확인
    existing_port = None
    if isinstance(virtual_service["spec"]["tcp"], list):
        for route in virtual_service["spec"]["tcp"]:
            destinations = route["route"]
            for destination in destinations:
                host = destination["destination"]["host"]
                if host == new_host:
                    matches = route.get("match") or []
                    if matches and isinstance(matches[0].get("port"), int):
                        existing_port = matches[0]["port"]
                    break

        if existing_port is None:
            virtual_service["spec"]["tcp"].append(new_route)
    else:
        virtual_service["spec"]["tcp"] = [new_route]

    # VirtualService 업데이트 또는 생성
    try:
        api_instance.patch_namespaced_custom_object(
            group="networking.istio.io",
            version="v1alpha3",
            namespace=namespace,
            plural="virtualservices",
            name=virtual_service_name,
            body=virtual_service,
        )
        logging.info(f"Updated Istio VirtualService for task_id: {task_id}")
    except client.exceptions.ApiException as e:
        if e.status == 404:
            try:
                api_instance.create_namespaced_custom_object(
                    group="networking.istio.io",
                    version="v1alpha3",
                    namespace=namespace,
                    plural="virtualservices",
                    body=virtual_service,
                )
                logging.info(f"Created Istio VirtualService for task_id: {task_id}")
            except client.exceptions.ApiException as e_create:
                raise e_create
        else:
            raise e
    allocated_port = existing_port if existing_port is not None else port
    if existing_port is not None and existing_port != port:
        logging.info(
            "Reused existing Task ID route for %s: requested=%s actual=%s",
            task_id,
            port,
            existing_port,
        )
    return allocated_port


def delete_fl_stack(task_id: str, namespace: str = TARGET_NAMESPACE, delete_pv: bool = True) -> Dict[str, Any]:
    """
    task_id 기준으로 Kubernetes 리소스(Deployment, Service, PVC, PV)를 삭제한다.
    - 이름 패턴 + 라벨(task_id={task_id}) 둘 다 활용
    - PVC가 바인딩한 PV를 찾아서 옵션(delete_pv=True)이면 삭제
    - 삭제 결과를 상세 리포트로 반환
    """
    load_config()
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()
    delete_opts = V1DeleteOptions(propagation_policy="Foreground", grace_period_seconds=0)

    # 네이밍 컨벤션 (이 리포지토리의 생성 로직 기준)
    deployment_name = f"fl-server-deploy-{task_id}"
    service_name    = fl_server_service_name(task_id)   # create_service_for_deployment에서 사용
    pvc_name        = f"fl-data-{task_id}"

    report: Dict[str, Any] = {
        "task_id": task_id,
        "namespace": namespace,
        "deleted": [],
        "preserved": [],
        "not_found": [],
        "errors": [],
    }

    # ---- Service 삭제 ----
    try:
        core_v1.delete_namespaced_service(name=service_name, namespace=namespace, body=delete_opts)
        report["deleted"].append({"service": service_name})
    except ApiException as e:
        if e.status == 404:
            # 라벨 보조 탐색
            try:
                svcs = core_v1.list_namespaced_service(namespace=namespace, label_selector=f"task_id={task_id}")
                if svcs.items:
                    for s in svcs.items:
                        try:
                            core_v1.delete_namespaced_service(name=s.metadata.name, namespace=namespace, body=delete_opts)
                            report["deleted"].append({"service": s.metadata.name})
                        except ApiException as ie:
                            report["errors"].append({"service": {"name": s.metadata.name, "error": ie.reason}})
                else:
                    report["not_found"].append({"service": service_name})
            except ApiException as le:
                report["errors"].append({"service_list": le.reason})
        else:
            report["errors"].append({"service": {"name": service_name, "error": e.reason}})

    # ---- Deployment 삭제 ----
    try:
        apps_v1.delete_namespaced_deployment(name=deployment_name, namespace=namespace, body=delete_opts)
        report["deleted"].append({"deployment": deployment_name})
    except ApiException as e:
        if e.status == 404:
            try:
                deps = apps_v1.list_namespaced_deployment(namespace=namespace, label_selector=f"task_id={task_id}")
                if deps.items:
                    for d in deps.items:
                        try:
                            apps_v1.delete_namespaced_deployment(name=d.metadata.name, namespace=namespace, body=delete_opts)
                            report["deleted"].append({"deployment": d.metadata.name})
                        except ApiException as ie:
                            report["errors"].append({"deployment": {"name": d.metadata.name, "error": ie.reason}})
                else:
                    report["not_found"].append({"deployment": deployment_name})
            except ApiException as le:
                report["errors"].append({"deployment_list": le.reason})
        else:
            report["errors"].append({"deployment": {"name": deployment_name, "error": e.reason}})

    # ---- PVC 삭제 & PV 후보 확보 ----
    bound_pv_names: List[str] = []

    def _append_pv_name_from_pvc(pvc_obj):
        try:
            vol_name = getattr(pvc_obj.spec, "volume_name", None)
            if vol_name:
                bound_pv_names.append(vol_name)
                report.setdefault("pvc_bound_pv", []).append({pvc_obj.metadata.name: vol_name})
        except Exception as _:
            pass

    pvc_found_any = False
    try:
        pvc = core_v1.read_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace)
        pvc_found_any = True
        _append_pv_name_from_pvc(pvc)
        if delete_pv:
            try:
                core_v1.delete_namespaced_persistent_volume_claim(name=pvc_name, namespace=namespace, body=delete_opts)
                report["deleted"].append({"pvc": pvc_name})
            except ApiException as e:
                if e.status == 404:
                    report["not_found"].append({"pvc": pvc_name})
                else:
                    report["errors"].append({"pvc": {"name": pvc_name, "error": e.reason}})
        else:
            report["preserved"].append({"pvc": pvc_name})
    except ApiException as e:
        if e.status == 404:
            # 라벨 보조
            try:
                pvcs = core_v1.list_namespaced_persistent_volume_claim(namespace=namespace, label_selector=f"task_id={task_id}")
                if pvcs.items:
                    pvc_found_any = True
                    for p in pvcs.items:
                        _append_pv_name_from_pvc(p)
                        if delete_pv:
                            try:
                                core_v1.delete_namespaced_persistent_volume_claim(name=p.metadata.name, namespace=namespace, body=delete_opts)
                                report["deleted"].append({"pvc": p.metadata.name})
                            except ApiException as ie:
                                report["errors"].append({"pvc": {"name": p.metadata.name, "error": ie.reason}})
                        else:
                            report["preserved"].append({"pvc": p.metadata.name})
                else:
                    report["not_found"].append({"pvc": pvc_name})
            except ApiException as le:
                report["errors"].append({"pvc_list": le.reason})
        else:
            report["errors"].append({"pvc": {"name": pvc_name, "error": e.reason}})

    # ---- PV 삭제 (옵션) ----
    # Dynamic volumes follow the provisioner's reclaim policy after PVC deletion.
    if delete_pv and os.getenv("FEDOPS_TASK_STORAGE_MODE", "local") == "local":
        try:
            v1 = core_v1
            # 1) PVC에서 얻은 바운드 PV들 우선 삭제
            for pv_name in set(bound_pv_names):
                try:
                    v1.delete_persistent_volume(name=pv_name, body=delete_opts)
                    report["deleted"].append({"pv": pv_name})
                except ApiException as e:
                    if e.status != 404:
                        report["errors"].append({"pv": {"name": pv_name, "error": e.reason}})

            # 2) 보조: claimRef로 PVC 이름/네임스페이스 매칭되는 PV 검색 후 삭제
            # PVC를 못 찾았더라도, 기존 이름 패턴이나 라벨이 다를 수 있어 전체 PV 스캔으로 폴백
            try:
                all_pvs = v1.list_persistent_volume().items
                for pv in all_pvs:
                    claim = getattr(pv.spec, "claim_ref", None) or getattr(pv.spec, "claimRef", None)
                    if claim and claim.name and claim.namespace:
                        if claim.namespace == namespace and claim.name == pvc_name:
                            # 라벨링된 PVC를 여러 개 삭제한 경우도 포함
                            try:
                                v1.delete_persistent_volume(name=pv.metadata.name, body=delete_opts)
                                report["deleted"].append({"pv": pv.metadata.name})
                            except ApiException as ie:
                                if ie.status != 404:
                                    report["errors"].append({"pv": {"name": pv.metadata.name, "error": ie.reason}})
            except ApiException as le:
                report["errors"].append({"pv_list": le.reason})

        except Exception as e:
            report["errors"].append({"pv": str(e)})
    # ---- VirtualService의 해당 route 제거 (포트 해제) ----
    try:
        co_api = client.CustomObjectsApi()
        vs_name = VIRTUAL_SERVICE_NAME
        try:
            vs = co_api.get_namespaced_custom_object(
                group="networking.istio.io", version="v1alpha3",
                namespace=namespace, plural="virtualservices", name=vs_name
            )
        except client.exceptions.ApiException as e:
            if e.status == 404:
                vs = None
        if vs and "tcp" in vs.get("spec", {}) and isinstance(vs["spec"]["tcp"], list):
            target_host = f"{service_name}.{namespace}.svc.cluster.local"
            new_tcp = []
            for route in vs["spec"]["tcp"]:
                # route.match[0].port 와 route.route[0].destination.host 검사
                dests = route.get("route", [])
                hosts = [d.get("destination", {}).get("host") for d in dests if d.get("destination")]
                if target_host in hosts:
                    # 이 route는 삭제 대상 -> 건너뜀
                    continue
                new_tcp.append(route)
            if new_tcp:
                vs["spec"]["tcp"] = new_tcp
                co_api.patch_namespaced_custom_object(
                    group="networking.istio.io", version="v1alpha3",
                    namespace=namespace, plural="virtualservices", name=vs_name, body=vs
                )
            else:
                # 더 이상 tcp 항목이 없으면 VirtualService 자체 삭제
                co_api.delete_namespaced_custom_object(
                    group="networking.istio.io", version="v1alpha3",
                    namespace=namespace, plural="virtualservices", name=vs_name, body=client.V1DeleteOptions()
                )
    except Exception as e:
        report["errors"].append({"virtualservice": str(e)})
    return report
