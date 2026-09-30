"""Recover scalable FL runtime metadata from Kubernetes resources.

The Server Manager keeps transient FL state in memory, but the Deployment,
Service, PVC, and Istio route outlive a Server Manager Pod.  This module
reconstructs the allocation metadata after a process or Pod restart.
"""

from __future__ import annotations
from utils.deployment_config import TARGET_NAMESPACE, VIRTUAL_SERVICE_NAME, ISTIO_GATEWAY, ISTIO_HOST, TASK_IMAGE, TASK_S3_SECRET_NAME, task_connection_env, required_url


import datetime
import json
import logging
from typing import Any, Dict, Optional

from utils.network_config import FL_SERVER_PORT_MAX, FL_SERVER_PORT_MIN
from utils.resource_names import fl_server_service_name


LOGGER = logging.getLogger(__name__)
VIRTUAL_SERVICE_GROUP = "networking.istio.io"
VIRTUAL_SERVICE_VERSION = "v1alpha3"


def load_kubernetes_config() -> None:
    """Load in-cluster config, falling back to the local development config."""
    from kubernetes import config
    from kubernetes.config.config_exception import ConfigException

    try:
        config.load_incluster_config()
    except ConfigException:
        config.load_kube_config("config.txt")


def find_service_route_port(
    virtual_service: Dict[str, Any],
    service_name: str,
    namespace: str,
) -> Optional[int]:
    """Return the gateway port that routes to a Task Service."""
    expected_hosts = {
        service_name,
        f"{service_name}.{namespace}",
        f"{service_name}.{namespace}.svc",
        f"{service_name}.{namespace}.svc.cluster.local",
    }

    for tcp_route in virtual_service.get("spec", {}).get("tcp", []) or []:
        destinations = tcp_route.get("route", []) or []
        hosts = {
            destination.get("destination", {}).get("host")
            for destination in destinations
        }
        if not hosts.intersection(expected_hosts):
            continue

        for match in tcp_route.get("match", []) or []:
            port = match.get("port")
            if isinstance(port, int) and FL_SERVER_PORT_MIN <= port <= FL_SERVER_PORT_MAX:
                return port

    return None


def service_external_ip(service: Any) -> Optional[str]:
    """Read the first LoadBalancer IP or hostname from a Service."""
    public_host = __import__("os").getenv("FEDOPS_TASK_PUBLIC_HOST", "").strip()
    if public_host:
        return public_host
    ingress = getattr(
        getattr(getattr(service, "status", None), "load_balancer", None),
        "ingress",
        None,
    ) or []
    if not ingress:
        return None
    return getattr(ingress[0], "ip", None) or getattr(ingress[0], "hostname", None)


def deployment_resource_requests(deployment: Any) -> Dict[str, Optional[str]]:
    """Return CPU and memory requests from the first runtime container."""
    containers = (
        getattr(
            getattr(
                getattr(getattr(deployment, "spec", None), "template", None),
                "spec",
                None,
            ),
            "containers",
            None,
        )
        or []
    )
    if not containers:
        return {"cpu": None, "memory": None}

    resources = getattr(containers[0], "resources", None)
    requests = getattr(resources, "requests", None) or {}
    limits = getattr(resources, "limits", None) or {}
    return {
        "cpu": requests.get("cpu") or limits.get("cpu"),
        "memory": requests.get("memory") or limits.get("memory"),
    }


def deployment_has_yaml_config(deployment: Any) -> bool:
    """Return whether the runtime Deployment contains a non-empty FL YAML."""
    containers = (
        getattr(
            getattr(
                getattr(getattr(deployment, "spec", None), "template", None),
                "spec",
                None,
            ),
            "containers",
            None,
        )
        or []
    )
    if not containers:
        return False

    for env_var in getattr(containers[0], "env", None) or []:
        if getattr(env_var, "name", None) == "FL_YAML_CONFIG":
            return bool(getattr(env_var, "value", None))
    return False


def deployment_campaign(deployment: Any) -> Optional[Dict[str, Any]]:
    """Recover the immutable Campaign overlay stored on the runtime container."""
    containers = (
        getattr(
            getattr(
                getattr(getattr(deployment, "spec", None), "template", None),
                "spec",
                None,
            ),
            "containers",
            None,
        )
        or []
    )
    if not containers:
        return None
    for env_var in getattr(containers[0], "env", None) or []:
        if getattr(env_var, "name", None) != "FEDOPS_CAMPAIGN_CONFIG":
            continue
        try:
            value = json.loads(getattr(env_var, "value", "") or "")
        except (TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def deployment_release_identity(deployment: Any) -> Dict[str, Optional[str]]:
    """Recover the exact Published Release identity from the Pod template."""
    annotations = (
        getattr(
            getattr(getattr(getattr(deployment, "spec", None), "template", None), "metadata", None),
            "annotations",
            None,
        )
        or {}
    )
    return {
        "release_id": annotations.get("fedops.io/release-id"),
        "release_sha256": annotations.get("fedops.io/release-sha256"),
    }


def inferred_runtime_status(deployment: Any) -> str:
    """Infer a conservative lifecycle state from Deployment readiness."""
    desired = getattr(getattr(deployment, "spec", None), "replicas", None) or 0
    ready = getattr(getattr(deployment, "status", None), "ready_replicas", None) or 0
    if desired == 0:
        return "FL Server Paused"
    if ready >= desired:
        # Kubernetes readiness cannot prove that server_main.py is training.
        return "FL Server created"
    return "FL Server Creating"


def build_runtime_status(
    *,
    task_id: str,
    deployment: Any,
    service: Any,
    pvc: Any,
    port: Optional[int],
) -> Dict[str, Any]:
    """Build the in-memory status representation used by existing APIs."""
    resources = deployment_resource_requests(deployment)
    release = deployment_release_identity(deployment)
    return {
        "status": inferred_runtime_status(deployment),
        "deployment": deployment.metadata.name,
        "service_name": service.metadata.name,
        "pvc": pvc.metadata.name if pvc is not None else f"fl-data-{task_id}",
        "cpu": resources["cpu"],
        "memory": resources["memory"],
        "port": port,
        "external_ip": service_external_ip(service),
        "yaml_saved": deployment_has_yaml_config(deployment),
        "campaign": deployment_campaign(deployment),
        **release,
        "recovered_from": "kubernetes",
        "recovered_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def merge_runtime_status(
    current: Dict[str, Any], recovered: Dict[str, Any]
) -> Dict[str, Any]:
    """Refresh durable allocation fields while preserving live process state."""
    dynamic_status = current.get("status")
    current.update(recovered)
    if dynamic_status:
        current["status"] = dynamic_status
    return current


def recover_runtime_status(
    task_id: str,
    namespace: str = TARGET_NAMESPACE,
) -> Optional[Dict[str, Any]]:
    """Recover one Task allocation, or return None when no Deployment exists."""
    from kubernetes import client

    load_kubernetes_config()
    apps_v1 = client.AppsV1Api()
    core_v1 = client.CoreV1Api()
    custom_objects = client.CustomObjectsApi()

    deployment_name = f"fl-server-deploy-{task_id}"
    service_name = fl_server_service_name(task_id)
    pvc_name = f"fl-data-{task_id}"

    try:
        deployment = apps_v1.read_namespaced_deployment(deployment_name, namespace)
    except client.exceptions.ApiException as error:
        if error.status == 404:
            return None
        raise

    try:
        service = core_v1.read_namespaced_service(service_name, namespace)
    except client.exceptions.ApiException as error:
        if error.status == 404:
            LOGGER.warning("Runtime Service missing for task %s", task_id)
            return None
        raise

    try:
        pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
    except client.exceptions.ApiException as error:
        if error.status == 404:
            pvc = None
        else:
            raise

    try:
        virtual_service = custom_objects.get_namespaced_custom_object(
            group=VIRTUAL_SERVICE_GROUP,
            version=VIRTUAL_SERVICE_VERSION,
            namespace=namespace,
            plural="virtualservices",
            name=VIRTUAL_SERVICE_NAME,
        )
        port = find_service_route_port(virtual_service, service_name, namespace)
    except client.exceptions.ApiException as error:
        if error.status == 404:
            port = None
        else:
            raise

    return build_runtime_status(
        task_id=task_id,
        deployment=deployment,
        service=service,
        pvc=pvc,
        port=port,
    )


def reconcile_runtime_statuses(
    status_store: Dict[str, Dict[str, Any]],
    namespace: str = TARGET_NAMESPACE,
) -> Dict[str, Dict[str, Any]]:
    """Recover every scalable Task Deployment into the process status store."""
    from kubernetes import client

    load_kubernetes_config()
    apps_v1 = client.AppsV1Api()
    deployments = apps_v1.list_namespaced_deployment(
        namespace=namespace,
        label_selector="app=fl-server",
    )
    recovered: Dict[str, Dict[str, Any]] = {}

    for deployment in deployments.items:
        labels = deployment.metadata.labels or {}
        task_id = labels.get("task_id")
        if not task_id:
            prefix = "fl-server-deploy-"
            name = deployment.metadata.name or ""
            task_id = name[len(prefix):] if name.startswith(prefix) else None
        if not task_id:
            continue

        try:
            runtime = recover_runtime_status(task_id, namespace)
        except Exception:
            LOGGER.exception("Failed to recover runtime metadata for task %s", task_id)
            continue
        if runtime is None:
            continue

        current = status_store.get(task_id)
        if current:
            recovered[task_id] = merge_runtime_status(current, runtime)
        else:
            status_store[task_id] = runtime
            recovered[task_id] = runtime

    return recovered
