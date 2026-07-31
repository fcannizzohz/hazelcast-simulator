import atexit
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from copy import deepcopy
from os import path
from urllib.error import URLError
from urllib.request import urlopen

import yaml

from simulator.log import info, warn
from simulator.util import exit_with_error, mkdir, write_yaml


GENERATED_DIR = ".simulator-k8s"
GENERATED_MANIFEST = f"{GENERATED_DIR}/generated.yaml"
PROVIDER_STATE = f"{GENERATED_DIR}/provider-state.yaml"
OWNER_LABEL = "simulator.hazelcast.com/managed"
INSTANCE_LABEL = "simulator.hazelcast.com/instance"
ROLE_LABEL = "simulator.hazelcast.com/role"
DEFAULT_TOPOLOGY_KEY = "topology.kubernetes.io/zone"
SUPPORTED_PROVIDERS = ("gke", "existing")
SUPPORTED_SERVICE_TYPES = ("ClusterIP", "LoadBalancer", "NodePort")
_PORT_FORWARDS = {}


def kubernetes_apply(inventory_plan, force=False):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    provider = kubernetes_provider(inventory_plan)
    if force:
        if provider == "gke":
            _gke_apply(inventory_plan, force=False)
        _delete_rendered_manifests(inventory_plan)
        if provider == "gke":
            _gke_destroy(inventory_plan, force=True)
    if provider == "gke":
        _gke_apply(inventory_plan, force)
    elif provider == "existing":
        info(f"Using existing Kubernetes cluster for provider [{provider}]")
    else:
        exit_with_error(f"Unsupported Kubernetes provider [{provider}]")
    kubernetes_import(inventory_plan)


def kubernetes_destroy(inventory_plan, force=False):
    validate_inventory_plan(inventory_plan, require_license=False, require_simulator=False)
    _require_tool("kubectl")
    provider = kubernetes_provider(inventory_plan)
    cluster_accessible = True
    if provider == "gke":
        _require_tool("gcloud")
        cluster_accessible = (
            _gke_get_credentials(inventory_plan, check=False).returncode == 0
            and _cluster_accessible(inventory_plan)
        )
    elif not _cluster_accessible(inventory_plan):
        exit_with_error(
            "Kubernetes API is unreachable; refusing to discard local ownership state before in-cluster cleanup"
        )
    if cluster_accessible:
        from simulator.chaos_kubernetes import cleanup_owned_chaos
        cleanup_owned_chaos(inventory_plan)
        _delete_rendered_manifests(inventory_plan)
        _uninstall_managed_addons(inventory_plan)
    else:
        if not _gke_delete_allowed(inventory_plan):
            exit_with_error(
                "GKE cluster is unreachable and is not owned by this project; refusing to discard local ownership "
                "state before in-cluster cleanup"
            )
        warn("GKE cluster is unavailable; skipping in-cluster resource cleanup")
    if provider == "gke":
        _gke_destroy(inventory_plan, force)
    elif provider == "existing":
        info(f"Leaving existing Kubernetes cluster intact for provider [{provider}]")
    else:
        exit_with_error(f"Unsupported Kubernetes provider [{provider}]")
    if path.exists("inventory.yaml"):
        os.remove("inventory.yaml")
    if path.isdir(GENERATED_DIR):
        shutil.rmtree(GENERATED_DIR)


def kubernetes_import(inventory_plan):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    _verify_cluster_access(inventory_plan)
    inventory = generate_inventory(inventory_plan)
    info("Creating [inventory.yaml]")
    write_yaml("inventory.yaml", inventory)


def kubernetes_install(inventory_plan):
    validate_inventory_plan(inventory_plan)
    _require_tool("kubectl")
    mkdir(GENERATED_DIR)
    _ensure_namespace(inventory_plan)
    _verify_license_secret(inventory_plan)
    _ensure_operator(inventory_plan)
    manifests = render_manifests(inventory_plan)
    with open(GENERATED_MANIFEST, "w") as f:
        yaml.safe_dump_all(manifests, f, sort_keys=False)
    os.chmod(GENERATED_MANIFEST, 0o600)

    provider = kubernetes_provider(inventory_plan)
    if _chaosmesh_enabled(inventory_plan) and _chaosmesh_install(inventory_plan):
        _install_chaosmesh(inventory_plan)
    if _chaosmesh_enabled(inventory_plan):
        _verify_chaosmesh(inventory_plan)

    _verify_resource_ownership(inventory_plan, manifests)
    _apply_rendered_manifests(inventory_plan)
    _wait_for_hazelcast(inventory_plan)
    _wait_for_supporting_workloads(inventory_plan)
    _verify_observability(inventory_plan)
    _verify_dc_distribution(inventory_plan)
    kubernetes_import(inventory_plan)
    _print_endpoints(inventory_plan)
    info(f"Kubernetes install complete for provider [{provider}]")


def kubernetes_provider(inventory_plan):
    kubernetes = inventory_plan.get("kubernetes") or {}
    provider = kubernetes.get("provider", "existing")
    # static was the original name for an attached Kubernetes cluster.
    return "existing" if provider == "static" else provider


def namespace(inventory_plan):
    return (inventory_plan.get("kubernetes") or {}).get("namespace", "default")


def kube_context(inventory_plan):
    return (inventory_plan.get("kubernetes") or {}).get("context")


def cluster_name(inventory_plan):
    return (inventory_plan.get("hazelcast") or {}).get("cluster_name", "workers")


def hazelcast_resource_name(inventory_plan):
    return (inventory_plan.get("hazelcast") or {}).get("name", "hazelcast")


def management_center_name(inventory_plan):
    return (inventory_plan.get("mc") or {}).get("name", "management-center")


def observability_enabled(inventory_plan):
    return bool((inventory_plan.get("observability") or {}).get("enabled", False))


def service_exposure(inventory_plan):
    return (inventory_plan.get("kubernetes") or {}).get("service_type", "ClusterIP")


def instance_name(inventory_plan):
    configured = (inventory_plan.get("kubernetes") or {}).get("instance")
    value = configured or f"{namespace(inventory_plan)}-{hazelcast_resource_name(inventory_plan)}"
    value = re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-.")
    return value[:63] or "simulator"


def topology_key(inventory_plan):
    return (inventory_plan.get("kubernetes") or {}).get("topology_key", DEFAULT_TOPOLOGY_KEY)


def validate_inventory_plan(inventory_plan, require_license=True, require_simulator=True):
    if inventory_plan.get("provisioner") != "kubernetes":
        exit_with_error("Kubernetes inventory requires provisioner: kubernetes")

    provider = kubernetes_provider(inventory_plan)
    if provider not in SUPPORTED_PROVIDERS:
        exit_with_error(f"Unsupported Kubernetes provider [{provider}]")

    exposure = service_exposure(inventory_plan)
    if exposure not in SUPPORTED_SERVICE_TYPES:
        exit_with_error(f"Unsupported Kubernetes service_type [{exposure}]")
    hz = inventory_plan.get("hazelcast") or {}
    external = hz.get("external") or {}
    if external.get("type", "Smart") not in ("Smart", "Unisocket"):
        exit_with_error("hazelcast.external.type must be Smart or Unisocket")
    if external.get("discovery_service_type", "LoadBalancer") not in ("LoadBalancer", "NodePort"):
        exit_with_error("hazelcast.external.discovery_service_type must be LoadBalancer or NodePort")
    size = _positive_int(hz.get("cluster_size", 3), "hazelcast.cluster_size")
    member_access = external.get("member_access")
    if member_access and member_access not in ("NodePortExternalIP", "NodePortNodeName", "LoadBalancer"):
        exit_with_error(
            "hazelcast.external.member_access must be NodePortExternalIP, NodePortNodeName, or LoadBalancer"
        )

    dcs = _dc_plans(inventory_plan, size)
    names = [dc.get("name") for dc in dcs]
    if any(not name for name in names) or len(set(names)) != len(names):
        exit_with_error("Each dcs entry must have a unique non-empty name")
    counts = [_positive_int(dc.get("members", 0), "dcs.members") for dc in dcs]
    if sum(counts) != size:
        exit_with_error("dcs member counts must be positive and sum to hazelcast.cluster_size")
    if len(dcs) > 1:
        ordinal_entries = [dc.get("pod_ordinals") for dc in dcs]
        if any(entry is not None for entry in ordinal_entries):
            if any(not isinstance(entry, list) for entry in ordinal_entries):
                exit_with_error("All dcs entries must define pod_ordinals when synthetic logical regions are used")
            ordinals = []
            for dc, count, entry in zip(dcs, counts, ordinal_entries):
                if len(entry) != count or any(not isinstance(item, int) or item < 0 for item in entry):
                    exit_with_error(
                        f"dcs.{dc['name']}.pod_ordinals must contain exactly {count} non-negative integers"
                    )
                ordinals.extend(entry)
            if sorted(ordinals) != list(range(size)):
                exit_with_error("Synthetic logical-region pod_ordinals must cover every StatefulSet ordinal exactly once")
        else:
            values = [dc.get("topology_value") for dc in dcs]
            if any(not value for value in values) or len(set(values)) != len(values):
                exit_with_error("Multiple dcs entries require unique topology_value settings")

    if require_simulator:
        simulator = inventory_plan.get("simulator") or {}
        if not simulator.get("image"):
            exit_with_error("Kubernetes inventory requires simulator.image")
        loadgenerators = simulator.get("loadgenerators") or {}
        _positive_int(loadgenerators.get("count", 1), "simulator.loadgenerators.count")
        if (inventory_plan.get("loadgenerators") or {}).get("hosts"):
            exit_with_error(
                "Kubernetes runs load generators in-cluster. Remove loadgenerators.hosts and set "
                "simulator.loadgenerators.count instead."
            )

    from simulator.chaos_kubernetes import validate_chaos_configuration
    validate_chaos_configuration(inventory_plan)

    storage = ((hz.get("persistence") or {}).get("storage_class"))
    if storage is not None and not isinstance(storage, dict):
        exit_with_error("hazelcast.persistence.storage_class must be a mapping")
    if hz.get("cp_enabled") and not (hz.get("persistence") or {}).get("enabled", False):
        exit_with_error("hazelcast.cp_enabled requires hazelcast.persistence.enabled: true for Kubernetes")

    if require_license:
        existing_license_secret = hz.get("existing_license_secret")
        if existing_license_secret is not None and (
                not isinstance(existing_license_secret, str) or not existing_license_secret.strip()):
            exit_with_error("hazelcast.existing_license_secret must be a non-empty Secret name")
        if not existing_license_secret and not _environment_license_key():
            _validate_input_file(hz.get("license_file"), "hazelcast.license_file", allow_empty=False)
        custom_config = hz.get("custom_config") or {}
        _validate_input_file(custom_config.get("file"), "hazelcast.custom_config.file", allow_empty=True)

    wait_timeout = (inventory_plan.get("kubernetes") or {}).get("wait_timeout_seconds", 600)
    _positive_int(wait_timeout, "kubernetes.wait_timeout_seconds")

    if provider == "gke":
        gke = inventory_plan.get("gke") or {}
        _required(gke, "project_id", "gke.project_id")
        _required(gke, "cluster_name", "gke.cluster_name")
        if not gke.get("region") and not gke.get("zone"):
            exit_with_error("Missing required setting [gke.zone or gke.region]")
        _positive_int(gke.get("node_count", 3), "gke.node_count")

    if require_license and not _configured_license_source(inventory_plan):
        exit_with_error(
            "Hazelcast Operator requires an Enterprise license. Configure hazelcast.existing_license_secret, "
            "HZ_LICENSEKEY, or hazelcast.license_file."
        )


def kubectl_base(inventory_plan):
    cmd = ["kubectl"]
    context = kube_context(inventory_plan)
    if context:
        cmd.extend(["--context", context])
    return cmd


def run_kubectl(inventory_plan, args, check=True, capture_output=False):
    cmd = kubectl_base(inventory_plan) + args
    info(" ".join(_redacted_command(cmd)))
    result = subprocess.run(cmd, text=True, capture_output=capture_output)
    if check and result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else ""
        display = " ".join(_redacted_command(cmd))
        exit_with_error(f"kubectl failed, exitcode={result.returncode}, command=[{display}], stderr=[{stderr}]")
    return result


def _redacted_command(cmd):
    result = []
    redact_next = False
    for item in cmd:
        if redact_next:
            key = item.split("=", 1)[0]
            result.append(f"{key}=<redacted>")
            redact_next = False
            continue
        result.append(str(item))
        redact_next = item == "--param"
    return result


def _cluster_accessible(inventory_plan):
    result = run_kubectl(
        inventory_plan,
        ["version", "--request-timeout=10s"],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _verify_cluster_access(inventory_plan):
    if not _cluster_accessible(inventory_plan):
        context = kube_context(inventory_plan) or "current"
        exit_with_error(f"Kubernetes API is unreachable for context [{context}]")


def render_manifests(inventory_plan):
    docs = []
    docs.extend(_license_manifests(inventory_plan))
    custom_config = _custom_config_manifest(inventory_plan)
    if custom_config:
        docs.append(custom_config)
    if _storage_class(inventory_plan):
        docs.append(_storage_class_manifest(inventory_plan))
    docs.append(_hazelcast_manifest(inventory_plan))
    if _pdb_enabled(inventory_plan):
        docs.append(_pdb_manifest(inventory_plan))
    if _mc_enabled(inventory_plan):
        docs.append(_management_center_manifest(inventory_plan))
    if observability_enabled(inventory_plan):
        docs.extend(_observability_manifests(inventory_plan))
    docs.extend(_simulator_runtime_manifests(inventory_plan))
    result = [doc for doc in docs if doc]
    for doc in result:
        metadata = doc.setdefault("metadata", {})
        labels = metadata.setdefault("labels", {})
        labels[OWNER_LABEL] = "true"
        labels[INSTANCE_LABEL] = instance_name(inventory_plan)
    return result


def generate_inventory(inventory_plan):
    inventory = {}
    node_hosts = _node_hosts(inventory_plan)
    inventory["nodes"] = {"hosts": node_hosts}
    for dc in _dc_names(inventory_plan):
        hosts = {name: host for name, host in node_hosts.items() if host.get("dc") == dc}
        if hosts:
            inventory[dc] = {"hosts": hosts}

    loadgenerators = _loadgenerator_hosts(inventory_plan)
    if loadgenerators:
        inventory["loadgenerators"] = {"hosts": loadgenerators}
        inventory["simulator_agents"] = {"hosts": deepcopy(loadgenerators)}

    hazelcast_endpoint = _cluster_service_endpoint(inventory_plan, hazelcast_resource_name(inventory_plan), 5701)
    inventory["hazelcast"] = {"hosts": {hazelcast_endpoint["host"]: hazelcast_endpoint["host_data"]}}

    mc_endpoint = _cluster_service_endpoint(inventory_plan, management_center_name(inventory_plan), 8080)
    if _mc_enabled(inventory_plan):
        inventory["mc"] = {"hosts": {mc_endpoint["host"]: mc_endpoint["host_data"]}}

    if observability_enabled(inventory_plan):
        grafana_endpoint = _cluster_service_endpoint(inventory_plan, "grafana", 3000)
        inventory["observability"] = {"hosts": {grafana_endpoint["host"]: grafana_endpoint["host_data"]}}

    inventory["kubernetes"] = {
        "hosts": {
            "cluster": {
                "provider": kubernetes_provider(inventory_plan),
                "namespace": namespace(inventory_plan),
                "context": kube_context(inventory_plan),
                "hazelcast_service": hazelcast_resource_name(inventory_plan),
                "management_center_service": management_center_name(inventory_plan),
            }
        }
    }
    return inventory


def control_probe(inventory_plan, hosts):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    pods = _hazelcast_pods(inventory_plan)
    services = _service_summary(inventory_plan)
    from simulator.chaos_kubernetes import chaos_status
    experiments = chaos_status(inventory_plan)
    selected = _selected_pods(inventory_plan, hosts)
    return {
        "provider": kubernetes_provider(inventory_plan),
        "namespace": namespace(inventory_plan),
        "selected_pods": selected,
        "pods": pods,
        "services": services,
        "chaos": experiments,
    }


def control_kill_members(inventory_plan, hosts, lapse_seconds, dry_run, start_spread_seconds=0):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    selected = _selected_pods(inventory_plan, hosts)
    if not selected:
        exit_with_error(f"No Kubernetes members matched [{hosts}]")
    if _chaosmesh_enabled(inventory_plan):
        if not dry_run:
            _verify_chaosmesh(inventory_plan)
        return _run_pod_chaos(
            inventory_plan, selected, lapse_seconds, start_spread_seconds, dry_run
        )
    if dry_run:
        return {
            "action": "delete-pods",
            "dry_run": True,
            "pods": [_pod_name(pod) for pod in selected],
            "start_spread_seconds": start_spread_seconds,
            "lapse_seconds": lapse_seconds,
        }
    for pod, offset in _pod_schedule(selected, start_spread_seconds):
        if offset:
            time.sleep(offset)
        name = _pod_name(pod)
        run_kubectl(inventory_plan, ["delete", "pod", name, "-n", namespace(inventory_plan)])
        if lapse_seconds:
            time.sleep(lapse_seconds)
        _wait_for_named_pod(inventory_plan, name)
    return {"action": "delete-pods", "dry_run": False, "pods": [_pod_name(pod) for pod in selected]}


def control_graceful_restart_members(inventory_plan, hosts, lapse_seconds, dry_run, start_spread_seconds=0):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    selected = _selected_pods(inventory_plan, hosts)
    if not selected:
        exit_with_error(f"No Kubernetes members matched [{hosts}]")
    if dry_run:
        return {"action": "graceful-pod-restart", "dry_run": True, "pods": [_pod_name(pod) for pod in selected]}
    for pod, offset in _pod_schedule(selected, start_spread_seconds):
        if offset:
            time.sleep(offset)
        name = _pod_name(pod)
        run_kubectl(inventory_plan, ["delete", "pod", name, "-n", namespace(inventory_plan)])
        if lapse_seconds:
            time.sleep(lapse_seconds)
        _wait_for_named_pod(inventory_plan, name)
    return {"action": "graceful-pod-restart", "dry_run": False, "pods": [_pod_name(pod) for pod in selected]}


def control_split_brain(inventory_plan, partitions, lapse_seconds, dry_run):
    validate_inventory_plan(inventory_plan, require_license=False)
    _require_tool("kubectl")
    if not _chaosmesh_enabled(inventory_plan):
        exit_with_error("Kubernetes split-brain requires chaosmesh.enabled: true")
    if not dry_run:
        _verify_chaosmesh(inventory_plan)
    partition_groups = _parse_partitions(partitions)
    selected_groups = [
        _selected_pods(inventory_plan, ",".join(group))
        for group in partition_groups
    ]
    if any(not group for group in selected_groups):
        exit_with_error("Each split-brain partition must resolve to at least one current Hazelcast pod")
    left_names = {_pod_name(pod) for pod in selected_groups[0]}
    right_names = {_pod_name(pod) for pod in selected_groups[1]}
    if left_names & right_names:
        exit_with_error("Split-brain partitions must not overlap")
    manifest = _network_chaos_manifest(inventory_plan, selected_groups, lapse_seconds)
    return _apply_temporary_chaos(inventory_plan, manifest, lapse_seconds, dry_run)


def management_center_endpoint(inventory_plan):
    _require_tool("kubectl")
    endpoint = _service_endpoint(inventory_plan, management_center_name(inventory_plan), 8080)
    if not endpoint:
        exit_with_error("Could not resolve Management Center endpoint from Kubernetes services/routes.")
    if endpoint.get("url"):
        return endpoint["url"]
    if endpoint["host"].endswith(".svc"):
        return _start_port_forward(inventory_plan, management_center_name(inventory_plan), 8080)
    port = endpoint["host_data"].get("port", 8080)
    return f"http://{endpoint['host']}:{port}"


def _gke_apply(inventory_plan, force):
    gke = inventory_plan.get("gke") or {}
    _require_tool("gcloud")
    project = _required(gke, "project_id", "gke.project_id")
    cluster = _required(gke, "cluster_name", "gke.cluster_name")
    location_flag, location = _gke_location(gke)
    identity = _gke_identity(project, cluster, location_flag, location)
    describe = [
        "gcloud", "container", "clusters", "describe", cluster,
        "--project", project, location_flag, location,
    ]
    exists = _run_cmd(describe, check=False, capture_output=True).returncode == 0
    previous_state = _read_provider_state()
    created = bool(previous_state.get("created") and _provider_state_identity(previous_state) == identity)
    if exists:
        info(f"Using existing GKE cluster [{cluster}]")
    elif not gke.get("create_cluster", True):
        exit_with_error(f"GKE cluster [{cluster}] does not exist and gke.create_cluster=false")
    else:
        machine_type = gke.get("node_machine_type", "c2-standard-4")
        node_count = str(gke.get("node_count", 3))
        cmd = [
            "gcloud", "container", "clusters", "create", cluster,
            "--project", project,
            location_flag, location,
            "--num-nodes", node_count,
            "--machine-type", machine_type,
            "--labels", f"simulator-managed=true,simulator-instance={instance_name(inventory_plan).replace('.', '-')}",
        ]
        if gke.get("node_locations"):
            cmd.extend(["--node-locations", ",".join(gke["node_locations"])])
        if gke.get("network"):
            cmd.extend(["--network", gke["network"]])
        if gke.get("subnetwork"):
            cmd.extend(["--subnetwork", gke["subnetwork"]])
        _run_cmd(cmd)
        created = True

    _gke_get_credentials(inventory_plan)
    _write_provider_state({**identity, "created": created})


def _gke_destroy(inventory_plan, force):
    gke = inventory_plan.get("gke") or {}
    project = _required(gke, "project_id", "gke.project_id")
    cluster = _required(gke, "cluster_name", "gke.cluster_name")
    location_flag, location = _gke_location(gke)
    if not _gke_delete_allowed(inventory_plan):
        info("Leaving GKE cluster intact because it was not created by this simulator project")
        return
    exists = _run_cmd([
        "gcloud", "container", "clusters", "describe", cluster,
        "--project", project, location_flag, location,
    ], check=False, capture_output=True).returncode == 0
    if not exists:
        info(f"GKE cluster [{cluster}] is already absent")
        return
    _run_cmd([
        "gcloud", "container", "clusters", "delete", cluster,
        "--project", project,
        location_flag, location,
        "--quiet",
    ])


def _gke_delete_allowed(inventory_plan):
    gke = inventory_plan.get("gke") or {}
    project = _required(gke, "project_id", "gke.project_id")
    cluster = _required(gke, "cluster_name", "gke.cluster_name")
    location_flag, location = _gke_location(gke)
    identity = _gke_identity(project, cluster, location_flag, location)
    state = _read_provider_state()
    owned_cluster = state.get("created") and _provider_state_identity(state) == identity
    return bool(owned_cluster or gke.get("delete_existing_cluster", False))


def _gke_get_credentials(inventory_plan, check=True):
    gke = inventory_plan.get("gke") or {}
    project = _required(gke, "project_id", "gke.project_id")
    cluster = _required(gke, "cluster_name", "gke.cluster_name")
    location_flag, location = _gke_location(gke)
    return _run_cmd([
        "gcloud", "container", "clusters", "get-credentials", cluster,
        "--project", project, location_flag, location,
    ], check=check, capture_output=not check)


def _gke_location(gke):
    if gke.get("region"):
        return "--region", gke["region"]
    return "--zone", _required(gke, "zone", "gke.zone or gke.region")


def _gke_identity(project, cluster, location_flag, location):
    return {
        "provider": "gke",
        "project": project,
        "cluster": cluster,
        "location_type": location_flag.removeprefix("--"),
        "location": location,
    }


def _provider_state_identity(state):
    return {
        key: state.get(key)
        for key in ("provider", "project", "cluster", "location_type", "location")
    }


def _write_provider_state(state):
    mkdir(GENERATED_DIR)
    with open(PROVIDER_STATE, "w") as f:
        yaml.safe_dump(state, f, sort_keys=False)


def _read_provider_state():
    if not path.exists(PROVIDER_STATE):
        return {}
    with open(PROVIDER_STATE) as f:
        return yaml.safe_load(f) or {}


def _ensure_namespace(inventory_plan):
    ns = namespace(inventory_plan)
    result = run_kubectl(inventory_plan, ["get", "namespace", ns, "-o", "json"], check=False, capture_output=True)
    if result.returncode == 0:
        return
    if not (inventory_plan.get("kubernetes") or {}).get("manage_namespace", True):
        exit_with_error(f"Kubernetes namespace [{ns}] does not exist and kubernetes.manage_namespace=false")
    run_kubectl(inventory_plan, ["create", "namespace", ns])
    run_kubectl(inventory_plan, [
        "label", "namespace", ns, f"{OWNER_LABEL}=true", f"{INSTANCE_LABEL}={instance_name(inventory_plan)}",
        "--overwrite",
    ])


def _ensure_operator(inventory_plan):
    operator = inventory_plan.get("operator") or {}
    if operator.get("install", True):
        _require_tool("helm")
        version = operator.get("version", "5.17.0")
        operator_namespace = operator.get("namespace", "hz-system")
        release = operator.get("release_name", "simulator-hazelcast-operator")
        _run_cmd(["helm", "repo", "add", "hazelcast", "https://hazelcast-charts.s3.amazonaws.com/"], check=False)
        _run_cmd(["helm", "repo", "update"])
        run_kubectl(inventory_plan, ["create", "namespace", operator_namespace], check=False)
        cmd = [
            "helm", "upgrade", "--install", release, "hazelcast/hazelcast-platform-operator",
            "--namespace", operator_namespace, "--version", version, "--set", "installCRDs=true",
        ]
        context = kube_context(inventory_plan)
        if context:
            cmd.extend(["--kube-context", context])
        for item in operator.get("helm_set", []):
            cmd.extend(["--set", str(item)])
        _run_cmd(cmd)
        _wait_for_selector_condition(
            inventory_plan, "deployment", "app.kubernetes.io/name=hazelcast-platform-operator",
            "available", operator_namespace,
        )

    for crd in ("hazelcasts.hazelcast.com", "managementcenters.hazelcast.com"):
        result = run_kubectl(inventory_plan, ["get", "crd", crd], check=False, capture_output=True)
        if result.returncode != 0:
            exit_with_error(
                f"Hazelcast Operator CRD [{crd}] is unavailable. Set operator.install=true or install a compatible Operator."
            )


def _verify_chaosmesh(inventory_plan, kinds=None):
    kinds = kinds or ("PodChaos", "NetworkChaos")
    plurals = {"Workflow": "workflows", "Schedule": "schedules"}
    crds = {f"{plurals.get(kind, kind.lower())}.chaos-mesh.org" for kind in kinds}
    for crd in sorted(crds):
        result = run_kubectl(inventory_plan, ["get", "crd", crd], check=False, capture_output=True)
        if result.returncode != 0:
            exit_with_error(
                f"Chaos Mesh CRD [{crd}] is unavailable. Set chaosmesh.install=true or install Chaos Mesh first."
            )


def _verify_resource_ownership(inventory_plan, manifests):
    for manifest in manifests:
        kind = manifest.get("kind")
        metadata = manifest.get("metadata") or {}
        name = metadata.get("name")
        if not kind or not name:
            continue
        existing = _resource_json(inventory_plan, kind, name, metadata.get("namespace"))
        if not existing:
            continue
        labels = (existing.get("metadata") or {}).get("labels") or {}
        if (labels.get(OWNER_LABEL) != "true"
                or labels.get(INSTANCE_LABEL) != instance_name(inventory_plan)):
            exit_with_error(
                f"Refusing to replace unowned Kubernetes resource [{kind}/{name}]. "
                f"Use a different kubernetes.instance or resource name."
            )


def _resource_is_owned(inventory_plan, kind, name, resource_namespace):
    existing = _resource_json(inventory_plan, kind, name, resource_namespace)
    if not existing:
        return False
    labels = (existing.get("metadata") or {}).get("labels") or {}
    return labels.get(OWNER_LABEL) == "true" and labels.get(INSTANCE_LABEL) == instance_name(inventory_plan)


def _resource_json(inventory_plan, kind, name, resource_namespace):
    args = ["get", kind.lower(), name, "-o", "json"]
    if resource_namespace:
        args.extend(["-n", resource_namespace])
    result = run_kubectl(inventory_plan, args, check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _apply_rendered_manifests(inventory_plan):
    with open(GENERATED_MANIFEST) as manifest_file:
        manifests = [doc for doc in yaml.safe_load_all(manifest_file) if doc]

    dashboard_manifests = [
        doc for doc in manifests
        if doc.get("kind") == "ConfigMap"
        and (doc.get("metadata") or {}).get("name") == "grafana-dashboards"
    ]
    if not dashboard_manifests:
        run_kubectl(inventory_plan, ["apply", "-f", GENERATED_MANIFEST])
        return

    regular_manifests = [doc for doc in manifests if doc not in dashboard_manifests]
    with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", dir=GENERATED_DIR, delete=False
    ) as regular_file, tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", dir=GENERATED_DIR, delete=False
    ) as dashboard_file:
        yaml.safe_dump_all(regular_manifests, regular_file, sort_keys=False)
        yaml.safe_dump_all(dashboard_manifests, dashboard_file, sort_keys=False)
        regular_path = regular_file.name
        dashboard_path = dashboard_file.name

    try:
        run_kubectl(inventory_plan, ["apply", "-f", regular_path])
        # The dashboard payload is larger than the client-side apply annotation
        # limit. Server-side apply stores field ownership without that annotation.
        run_kubectl(inventory_plan, [
            "apply", "--server-side", "--force-conflicts",
            "--field-manager=hazelcast-simulator-grafana",
            "-f", dashboard_path,
        ])
    finally:
        os.remove(regular_path)
        os.remove(dashboard_path)


def _delete_rendered_manifests(inventory_plan):
    if path.exists(GENERATED_MANIFEST):
        with open(GENERATED_MANIFEST) as f:
            manifests = [doc for doc in yaml.safe_load_all(f) if doc]
        for manifest in reversed(manifests):
            kind = manifest.get("kind")
            metadata = manifest.get("metadata") or {}
            name = metadata.get("name")
            if not kind or not name or kind == "Namespace":
                continue
            if not _resource_is_owned(inventory_plan, kind, name, metadata.get("namespace")):
                warn(f"Skipping deletion of unowned Kubernetes resource [{kind}/{name}]")
                continue
            args = ["delete", kind.lower(), name, "--ignore-not-found=true"]
            if metadata.get("namespace"):
                args.extend(["-n", metadata["namespace"]])
            run_kubectl(inventory_plan, args)
    else:
        warn(f"Generated manifest [{GENERATED_MANIFEST}] is unavailable; deleting only labelled owned resources")

    selector = f"{INSTANCE_LABEL}={instance_name(inventory_plan)},{OWNER_LABEL}=true"
    resource_types = [
        "deployment", "statefulset", "service", "serviceaccount", "role", "rolebinding",
        "configmap", "secret", "poddisruptionbudget",
    ]
    if _crd_available(inventory_plan, "managementcenters.hazelcast.com"):
        resource_types.append("managementcenter")
    if _crd_available(inventory_plan, "hazelcasts.hazelcast.com"):
        resource_types.append("hazelcast")
    for resource_type in resource_types:
        run_kubectl(inventory_plan, [
            "delete", resource_type, "-l", selector, "-n", namespace(inventory_plan), "--ignore-not-found=true",
        ])
    run_kubectl(inventory_plan, [
        "delete", "storageclass", "-l", selector, "--ignore-not-found=true",
    ])

    kubernetes = inventory_plan.get("kubernetes") or {}
    if kubernetes.get("delete_namespace_on_destroy", False):
        ns = namespace(inventory_plan)
        if _resource_is_owned(inventory_plan, "namespace", ns, None):
            run_kubectl(inventory_plan, ["delete", "namespace", ns, "--ignore-not-found=true"])


def _install_chaosmesh(inventory_plan):
    _require_tool("helm")
    chaos = inventory_plan.get("chaosmesh") or {}
    version = chaos.get("version", "2.7.2")
    _run_cmd(["helm", "repo", "add", "chaos-mesh", "https://charts.chaos-mesh.org"], check=False)
    _run_cmd(["helm", "repo", "update"])
    chaos_namespace = _chaos_namespace(inventory_plan)
    run_kubectl(inventory_plan, ["create", "namespace", chaos_namespace], check=False)
    release = chaos.get("release_name", "chaos-mesh")
    cmd = [
        "helm", "upgrade", "--install", release, "chaos-mesh/chaos-mesh",
        "-n", chaos_namespace,
        "--version", version,
    ]
    context = kube_context(inventory_plan)
    if context:
        cmd.extend(["--kube-context", context])
    runtime = chaos.get("runtime")
    socket_path = chaos.get("socket_path")
    if runtime:
        cmd.extend(["--set", f"chaosDaemon.runtime={runtime}"])
    if socket_path:
        cmd.extend(["--set", f"chaosDaemon.socketPath={socket_path}"])
    for item in chaos.get("helm_set", []):
        cmd.extend(["--set", str(item)])
    _run_cmd(cmd)
    _wait_for_selector_condition(
        inventory_plan, "pod", f"app.kubernetes.io/instance={release}", "ready", chaos_namespace,
    )


def _crd_available(inventory_plan, name):
    return run_kubectl(
        inventory_plan, ["get", "crd", name], check=False, capture_output=True
    ).returncode == 0


def _uninstall_managed_addons(inventory_plan):
    chaos = inventory_plan.get("chaosmesh") or {}
    if chaos.get("install") and chaos.get("uninstall_on_destroy", False):
        _require_tool("helm")
        cmd = [
            "helm", "uninstall", chaos.get("release_name", "chaos-mesh"),
            "-n", _chaos_namespace(inventory_plan), "--ignore-not-found",
        ]
        if kube_context(inventory_plan):
            cmd.extend(["--kube-context", kube_context(inventory_plan)])
        _run_cmd(cmd)

    operator = inventory_plan.get("operator") or {}
    if operator.get("install", True) and operator.get("uninstall_on_destroy", False):
        _require_tool("helm")
        cmd = [
            "helm", "uninstall", operator.get("release_name", "simulator-hazelcast-operator"),
            "-n", operator.get("namespace", "hz-system"), "--ignore-not-found",
        ]
        if kube_context(inventory_plan):
            cmd.extend(["--kube-context", kube_context(inventory_plan)])
        _run_cmd(cmd)


def _wait_for_hazelcast(inventory_plan):
    name = hazelcast_resource_name(inventory_plan)
    size = int((inventory_plan.get("hazelcast") or {}).get("cluster_size", 1))
    for index in range(size):
        _wait_for_named_pod(inventory_plan, f"{name}-{index}")
    timeout = int((inventory_plan.get("kubernetes") or {}).get("wait_timeout_seconds", 600))
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_kubectl(
            inventory_plan,
            ["get", "hazelcast", name, "-n", namespace(inventory_plan), "-o", "json"],
            capture_output=True,
        )
        status = json.loads(result.stdout).get("status") or {}
        if status.get("phase") == "Running":
            return
        if status.get("phase") == "Failed":
            exit_with_error(f"Hazelcast resource [{name}] failed: {status.get('message', 'unknown error')}")
        time.sleep(2)
    exit_with_error(f"Timed out waiting for Hazelcast resource [{name}] to report phase Running")


def _wait_for_supporting_workloads(inventory_plan):
    if _mc_enabled(inventory_plan):
        _wait_for_named_pod(inventory_plan, f"{management_center_name(inventory_plan)}-0")
    if observability_enabled(inventory_plan):
        for deployment in ("prometheus", "grafana"):
            _wait_for_resource_condition(inventory_plan, "deployment", deployment, "available", namespace(inventory_plan))
    _wait_for_selector_condition(
        inventory_plan, "pod", f"app={_simulator_agents_name(inventory_plan)}", "ready", namespace(inventory_plan)
    )


def _observability_http(inventory_plan, service_name, port, request_path):
    base_url = _start_port_forward(inventory_plan, service_name, port)
    with urlopen(f"{base_url}{request_path}", timeout=5) as response:
        return json.loads(response.read().decode("utf-8")) if request_path.startswith("/api/") else response.read()


def _verify_observability(inventory_plan):
    if not observability_enabled(inventory_plan):
        return
    if not _mc_enabled(inventory_plan):
        exit_with_error("observability.enabled requires mc.enabled so Prometheus has a metrics source")

    timeout = int((inventory_plan.get("kubernetes") or {}).get("wait_timeout_seconds", 600))
    deadline = time.time() + timeout
    last_error = "unknown error"
    while time.time() < deadline:
        try:
            metrics = _observability_http(inventory_plan, management_center_name(inventory_plan), 8080, "/metrics")
            if not metrics.strip():
                raise RuntimeError("Management Center /metrics returned an empty response")
            health = _observability_http(inventory_plan, "grafana", 3000, "/api/health")
            if health.get("database") not in (None, "ok"):
                raise RuntimeError(f"Grafana health is not ready: {health}")
            targets = _observability_http(inventory_plan, "prometheus", 9090, "/api/v1/targets?state=active")
            active = (targets.get("data") or {}).get("activeTargets") or []
            mc_targets = [target for target in active if (target.get("labels") or {}).get("job") == "hazelcast-mc"]
            if not any(target.get("health") == "up" for target in mc_targets):
                details = ", ".join(target.get("lastError", "target is not up") for target in mc_targets)
                raise RuntimeError(f"Prometheus hazelcast-mc target is not up: {details or 'target missing'}")
            info("Observability readiness and Prometheus Management Center scrape verified")
            return
        except (OSError, URLError, ValueError, RuntimeError) as error:
            last_error = str(error)
            time.sleep(2)
    exit_with_error(f"Timed out verifying observability readiness and metrics scrape: {last_error}")


def _verify_dc_distribution(inventory_plan):
    expected = {dc["name"]: int(dc["members"]) for dc in _dc_plans(inventory_plan)}
    observed = {name: 0 for name in expected}
    pods = _hazelcast_pods(inventory_plan)
    for pod in pods:
        dc = pod.get("dc")
        if dc in observed:
            observed[dc] += 1
    if _uses_synthetic_regions(inventory_plan) and observed != expected:
        exit_with_error(
            f"Hazelcast synthetic-region topology does not match dcs plan; expected={expected}, observed={observed}."
        )
    if not _uses_synthetic_regions(inventory_plan) and sorted(observed.values()) != sorted(expected.values()):
        exit_with_error(
            f"Hazelcast physical topology distribution does not match dcs plan; expected={expected}, observed={observed}. "
            f"Check kubernetes.topology_key and each dcs.topology_value."
        )


def _print_endpoints(inventory_plan):
    endpoints = [(hazelcast_resource_name(inventory_plan), 5701, "Hazelcast")]
    if _mc_enabled(inventory_plan):
        endpoints.append((management_center_name(inventory_plan), 8080, "Management Center"))
    if observability_enabled(inventory_plan):
        endpoints.extend((("grafana", 3000, "Grafana"), ("prometheus", 9090, "Prometheus")))
    for service, port, label in endpoints:
        endpoint = _service_endpoint(inventory_plan, service, port)
        if endpoint:
            info(f"{label}: {endpoint['host']}")


def _namespace_manifest(ns):
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": ns},
    }


def _license_manifests(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    if hz.get("existing_license_secret"):
        return []
    secret = _license_secret_name(inventory_plan)
    license_key = _environment_license_key()
    if not license_key and hz.get("license_file"):
        with open(os.path.expanduser(hz["license_file"])) as f:
            license_key = f.read().strip()
    if not license_key:
        return []
    return [{
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret, "namespace": namespace(inventory_plan)},
        "type": "Opaque",
        "stringData": {"license-key": license_key},
    }]


def _configured_license_source(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    return bool(hz.get("existing_license_secret") or _environment_license_key() or hz.get("license_file"))


def _environment_license_key():
    return (os.environ.get("HZ_LICENSEKEY") or "").strip()


def _license_secret_name(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    existing = hz.get("existing_license_secret")
    if isinstance(existing, str):
        return existing
    return hz.get("license_secret_name", "hazelcast-license")


def _verify_license_secret(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    if not hz.get("existing_license_secret"):
        return
    secret = _license_secret_name(inventory_plan)
    result = run_kubectl(
        inventory_plan,
        ["get", "secret", secret, "-n", namespace(inventory_plan), "-o", "json"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        exit_with_error(f"Configured existing license Secret [{secret}] does not exist in namespace [{namespace(inventory_plan)}]")
    try:
        secret_data = (json.loads(result.stdout).get("data") or {})
    except (json.JSONDecodeError, AttributeError, TypeError):
        exit_with_error(f"Could not read configured existing license Secret [{secret}]")
    if not secret_data.get("license-key"):
        exit_with_error(f"Configured existing license Secret [{secret}] does not contain data key [license-key]")


def _custom_config_manifest(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    custom_config = hz.get("custom_config") or {}
    filename = custom_config.get("file")
    if not filename:
        return None
    with open(os.path.expanduser(filename)) as f:
        config = f.read()
    try:
        parsed = yaml.safe_load(config)
    except yaml.YAMLError as error:
        exit_with_error(f"hazelcast.custom_config.file is not valid YAML: {error}")
    if not isinstance(parsed, dict) or "hazelcast" not in parsed:
        exit_with_error("hazelcast.custom_config.file must be a Hazelcast YAML file with a top-level hazelcast mapping")
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "hazelcast-custom-config", "namespace": namespace(inventory_plan)},
        "data": {"hazelcast.yaml": config},
    }


def _storage_class(inventory_plan):
    return ((inventory_plan.get("hazelcast") or {}).get("persistence") or {}).get("storage_class")


def _storage_class_manifest(inventory_plan):
    persistence = (inventory_plan.get("hazelcast") or {}).get("persistence") or {}
    storage = persistence.get("storage_class") or {}
    return {
        "apiVersion": "storage.k8s.io/v1",
        "kind": "StorageClass",
        "metadata": {"name": storage.get("name", "hazelcast-storage")},
        "provisioner": storage.get("provisioner", "kubernetes.io/no-provisioner"),
        "parameters": storage.get("parameters", {}),
        "volumeBindingMode": storage.get("volume_binding_mode", "WaitForFirstConsumer"),
        "allowVolumeExpansion": storage.get("allow_volume_expansion", True),
    }


def _hazelcast_manifest(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    resources = hz.get("resources") or {}
    persistence = hz.get("persistence") or {}
    spec = {
        "clusterSize": int(hz.get("cluster_size", 3)),
        "clusterName": cluster_name(inventory_plan),
        "repository": hz.get("repository", "docker.io/hazelcast/hazelcast-enterprise"),
        "version": str(hz.get("version", "5.6.0")),
        "licenseKeySecretName": _license_secret_name(inventory_plan),
        "jet": {"resourceUploadEnabled": True},
        "userCodeDeployment": {"clientEnabled": True},
        "resources": resources or {"requests": {"cpu": "2", "memory": "8Gi"}},
        "properties": hz.get("properties", {}),
    }
    env = deepcopy(hz.get("env") or [])
    if observability_enabled(inventory_plan) and not any(
            item.get("name") == "PROMETHEUS_PORT" for item in env
    ):
        # Kubernetes injects PROMETHEUS_PORT as tcp://host:port from the
        # Prometheus Service. The Hazelcast image treats that as a member-side
        # JMX exporter bind address, but this stack scrapes Management Center
        # instead, so explicitly disable the unused member exporter.
        env.append({"name": "PROMETHEUS_PORT", "value": ""})
    if env:
        spec["env"] = env
    if (hz.get("custom_config") or {}).get("file"):
        spec["customConfigCmName"] = "hazelcast-custom-config"
    if (hz.get("external") or {}).get("enabled", False):
        spec["exposeExternally"] = _external_exposure(inventory_plan)
    if hz.get("jvm"):
        spec["jvm"] = hz["jvm"]
    if persistence.get("enabled"):
        storage = persistence.get("storage_class") or {}
        spec["persistence"] = {
            "clusterDataRecoveryPolicy": persistence.get("cluster_data_recovery_policy", "PartialRecoveryMostComplete"),
            "pvc": {
                "accessModes": persistence.get("access_modes", ["ReadWriteOnce"]),
                "requestStorage": persistence.get("request_storage", "20Gi"),
                "storageClassName": persistence.get("storage_class_name", storage.get("name", "standard")),
            },
        }
    if hz.get("diagnostics"):
        spec["diagnostics"] = hz["diagnostics"]
    scheduling = _hazelcast_scheduling(inventory_plan)
    if scheduling:
        spec["scheduling"] = scheduling
    if len(_dc_plans(inventory_plan)) > 1 and topology_key(inventory_plan) == DEFAULT_TOPOLOGY_KEY \
            and not _uses_synthetic_regions(inventory_plan):
        spec["highAvailabilityMode"] = "ZONE"
    return {
        "apiVersion": "hazelcast.com/v1alpha1",
        "kind": "Hazelcast",
        "metadata": {"name": hazelcast_resource_name(inventory_plan), "namespace": namespace(inventory_plan)},
        "spec": spec,
    }


def _hazelcast_scheduling(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    scheduling = deepcopy(hz.get("scheduling") or {})
    if hz.get("node_selector"):
        scheduling["nodeSelector"] = hz["node_selector"]

    dcs = _dc_plans(inventory_plan)
    if _uses_synthetic_regions(inventory_plan):
        return scheduling

    topology_values = [dc.get("topology_value") for dc in dcs if dc.get("topology_value")]
    if topology_values:
        affinity = scheduling.setdefault("affinity", {})
        node_affinity = affinity.setdefault("nodeAffinity", {})
        required = node_affinity.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {})
        terms = required.setdefault("nodeSelectorTerms", [{"matchExpressions": []}])
        expression = {"key": topology_key(inventory_plan), "operator": "In", "values": topology_values}
        for term in terms:
            term.setdefault("matchExpressions", []).append(deepcopy(expression))

    if len(dcs) > 1:
        constraints = scheduling.setdefault("topologySpreadConstraints", [])
        constraints.append({
            "maxSkew": 1,
            "topologyKey": topology_key(inventory_plan),
            "whenUnsatisfiable": "DoNotSchedule",
            "labelSelector": {"matchLabels": {
                "app.kubernetes.io/name": "hazelcast",
                "app.kubernetes.io/instance": hazelcast_resource_name(inventory_plan),
                "app.kubernetes.io/managed-by": "hazelcast-platform-operator",
            }},
        })
    return scheduling


def _management_center_manifest(inventory_plan):
    mc = inventory_plan.get("mc") or {}
    spec = {
        "repository": mc.get("repository", "docker.io/hazelcast/management-center"),
        "version": str(mc.get("version", "5.9.0")),
        "licenseKeySecretName": _license_secret_name(inventory_plan),
        "externalConnectivity": {"type": _kubernetes_service_type(inventory_plan)},
        "hazelcastClusters": [{"address": hazelcast_resource_name(inventory_plan), "name": cluster_name(inventory_plan)}],
        "persistence": {"enabled": True, "size": mc.get("persistence_size", "1Gi")},
        "jvm": {"args": ["-Dhazelcast.mc.port=8080", "-Dhazelcast.mc.prometheusExporter.enabled=true"]},
        "resources": mc.get("resources", {"requests": {"cpu": "1", "memory": "2Gi"}}),
    }
    if mc.get("node_selector"):
        spec["scheduling"] = {"nodeSelector": mc["node_selector"]}
    return {
        "apiVersion": "hazelcast.com/v1alpha1",
        "kind": "ManagementCenter",
        "metadata": {"name": management_center_name(inventory_plan), "namespace": namespace(inventory_plan)},
        "spec": spec,
    }


def _pdb_manifest(inventory_plan):
    hz_name = hazelcast_resource_name(inventory_plan)
    pdb = (inventory_plan.get("hazelcast") or {}).get("pdb") or {}
    return {
        "apiVersion": "policy/v1",
        "kind": "PodDisruptionBudget",
        "metadata": {"name": f"{hz_name}-pdb", "namespace": namespace(inventory_plan)},
        "spec": {
            "maxUnavailable": pdb.get("max_unavailable", 1),
            "selector": {"matchLabels": {"app.kubernetes.io/instance": hz_name}},
        },
    }


def _observability_manifests(inventory_plan):
    ns = namespace(inventory_plan)
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "prometheus-config", "namespace": ns},
            "data": {"prometheus.yml": _prometheus_config(inventory_plan)},
        },
        _deployment(inventory_plan, "prometheus", "prom/prometheus:v2.51.2", 9090, {
            "config": {
                "configMap": "prometheus-config",
                "mountPath": "/etc/prometheus",
            },
        }, args=["--config.file=/etc/prometheus/prometheus.yml", "--web.enable-admin-api"]),
        _service(inventory_plan, "prometheus", 9090, "ClusterIP"),
        _grafana_datasource_manifest(inventory_plan),
        _grafana_provider_manifest(inventory_plan),
        _grafana_dashboard_configmap(inventory_plan),
        _deployment(inventory_plan, "grafana", "grafana/grafana:10.4.2", 3000, {
            "datasources": {
                "configMap": "grafana-datasources",
                "mountPath": "/etc/grafana/provisioning/datasources",
            },
            "providers": {
                "configMap": "grafana-providers",
                "mountPath": "/etc/grafana/provisioning/dashboards",
            },
            "dashboards": {
                "configMap": "grafana-dashboards",
                "mountPath": "/var/lib/grafana/dashboards",
            },
        }, env=[
            {"name": "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH", "value": "/var/lib/grafana/dashboards/simulator-run-context.json"},
            {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "true"},
            {"name": "GF_AUTH_ANONYMOUS_ORG_ROLE", "value": "Admin"},
            {"name": "GF_AUTH_DISABLE_LOGIN_FORM", "value": "true"},
        ]),
        _service(inventory_plan, "grafana", 3000, service_exposure(inventory_plan)),
    ]


def _deployment(inventory_plan, name, image, port, config_maps, env=None, args=None):
    volume_mounts = []
    volumes = []
    for volume_name, config in config_maps.items():
        volumes.append({"name": volume_name, "configMap": {"name": config["configMap"]}})
        volume_mounts.append({"name": volume_name, "mountPath": config["mountPath"]})
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": namespace(inventory_plan), "labels": {"app": name}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [{
                        "name": name,
                        "image": image,
                        "ports": [{"containerPort": port}],
                        "volumeMounts": volume_mounts,
                        "env": env or [],
                        "args": args or [],
                    }],
                    "volumes": volumes,
                },
            },
        },
    }


def _simulator_runtime_manifests(inventory_plan):
    ns = namespace(inventory_plan)
    name = _simulator_agents_name(inventory_plan)
    simulator = inventory_plan.get("simulator") or {}
    loadgenerators = simulator.get("loadgenerators") or {}
    labels = {
        "app": name,
        OWNER_LABEL: "true",
        INSTANCE_LABEL: instance_name(inventory_plan),
        ROLE_LABEL: "loadgenerator",
    }
    pod_spec = {
        "serviceAccountName": _simulator_service_account_name(inventory_plan),
        "terminationGracePeriodSeconds": 30,
        "containers": [{
            "name": "simulator-agent",
            "image": simulator["image"],
            "imagePullPolicy": simulator.get("image_pull_policy", "IfNotPresent"),
            "command": ["/bin/sh", "-lc"],
            "args": [
                "ordinal=${HOSTNAME##*-}; index=$((ordinal + 1)); "
                "exec /opt/simulator/bin/hidden/agent --addressIndex ${index} "
                "--publicAddress ${HOSTNAME}." + name + ".${POD_NAMESPACE}.svc --port 9000"
            ],
            "ports": [{"name": "agent", "containerPort": 9000}],
            "resources": loadgenerators.get("resources", {"requests": {"cpu": "2", "memory": "4Gi"}}),
            "env": [{"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}}],
            "volumeMounts": [{"name": "workers", "mountPath": "/opt/simulator/workers"}],
            "readinessProbe": {"tcpSocket": {"port": "agent"}, "periodSeconds": 5, "failureThreshold": 12},
        }],
        "volumes": [{"name": "workers", "emptyDir": {}}],
    }
    image_pull_secrets = simulator.get("image_pull_secrets") or []
    if image_pull_secrets:
        pod_spec["imagePullSecrets"] = [
            item if isinstance(item, dict) else {"name": item} for item in image_pull_secrets
        ]
    scheduling = deepcopy(loadgenerators.get("scheduling") or {})
    for key in ("nodeSelector", "affinity", "tolerations", "topologySpreadConstraints"):
        if scheduling.get(key) is not None:
            pod_spec[key] = scheduling[key]
    topology_values = [dc.get("topology_value") for dc in _dc_plans(inventory_plan) if dc.get("topology_value")]
    if topology_values:
        affinity = pod_spec.setdefault("affinity", {})
        node_affinity = affinity.setdefault("nodeAffinity", {})
        required = node_affinity.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {})
        terms = required.setdefault("nodeSelectorTerms", [{"matchExpressions": []}])
        expression = {"key": topology_key(inventory_plan), "operator": "In", "values": topology_values}
        for term in terms:
            term.setdefault("matchExpressions", []).append(deepcopy(expression))
    if not pod_spec.get("topologySpreadConstraints") and len(_dc_plans(inventory_plan)) > 1:
        pod_spec["topologySpreadConstraints"] = [{
            "maxSkew": 1,
            "topologyKey": topology_key(inventory_plan),
            "whenUnsatisfiable": "ScheduleAnyway",
            "labelSelector": {"matchLabels": {"app": name}},
        }]

    service_account = _simulator_service_account_name(inventory_plan)
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": service_account, "namespace": ns}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": service_account, "namespace": ns},
            "rules": [{"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "create", "delete"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": service_account, "namespace": ns},
            "subjects": [{"kind": "ServiceAccount", "name": service_account, "namespace": ns}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": service_account},
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "clusterIP": "None",
                "publishNotReadyAddresses": True,
                "selector": {"app": name},
                "ports": [{"name": "agent", "port": 9000, "targetPort": "agent"}],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": name, "namespace": ns},
            "spec": {
                "serviceName": name,
                "replicas": int(loadgenerators.get("count", 1)),
                "podManagementPolicy": "Parallel",
                "selector": {"matchLabels": {"app": name}},
                "template": {"metadata": {"labels": labels}, "spec": pod_spec},
            },
        },
    ]


def _service(inventory_plan, name, port, service_type):
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": namespace(inventory_plan)},
        "spec": {
            "type": service_type,
            "selector": {"app": name},
            "ports": [{"name": "http", "port": port, "targetPort": port}],
        },
    }




def _grafana_datasource_manifest(inventory_plan):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "grafana-datasources", "namespace": namespace(inventory_plan)},
        "data": {
            "datasources.yaml": yaml.safe_dump({
                "apiVersion": 1,
                "datasources": [{
                    "name": "Prometheus",
                    "uid": "prometheus",
                    "type": "prometheus",
                    "url": "http://prometheus:9090",
                    "access": "proxy",
                    "isDefault": True,
                }],
            }, sort_keys=False)
        },
    }


def _grafana_provider_manifest(inventory_plan):
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "grafana-providers", "namespace": namespace(inventory_plan)},
        "data": {
            "dashboard.yaml": yaml.safe_dump({
                "apiVersion": 1,
                "providers": [{
                    "name": "Hazelcast",
                    "orgId": 1,
                    "folder": "Hazelcast",
                    "type": "file",
                    "disableDeletion": False,
                    "updateIntervalSeconds": 30,
                    "allowUiUpdates": True,
                    "options": {"path": "/var/lib/grafana/dashboards"},
                }],
            }, sort_keys=False),
        },
    }


def _grafana_dashboard_configmap(inventory_plan):
    dashboards = {}
    dashboard_dir = os.path.join(os.environ.get("SIMULATOR_HOME", "."), "observability", "grafana", "dashboards")
    if os.path.isdir(dashboard_dir):
        for filename in sorted(os.listdir(dashboard_dir)):
            if filename.endswith(".json"):
                with open(os.path.join(dashboard_dir, filename)) as f:
                    dashboards[filename] = f.read()
    # Include every completed Simulator run currently available in the project.
    # The dashboard payload embeds its report data, so Grafana remains useful
    # after the local run directory is no longer mounted in the cluster.
    from simulator.perftest_report_grafana import ReportData, ReportDashboardGenerator
    runs_root = path.join(os.getcwd(), "runs")
    if path.isdir(runs_root):
        for test_name in sorted(os.listdir(runs_root)):
            test_dir = path.join(runs_root, test_name)
            if not path.isdir(test_dir):
                continue
            for timestamp in sorted(os.listdir(test_dir)):
                run_dir = path.join(test_dir, timestamp)
                if not path.isdir(run_dir) or not re.match(r"^\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2}$", timestamp):
                    continue
                report = ReportData(run_dir)
                if not path.isfile(path.join(report.path, "report.csv")):
                    continue
                for dashboard in ReportDashboardGenerator(report, f"Simulator Run {report.timestamp}").generate():
                    dashboards[f"{dashboard['uid']}.json"] = json.dumps(dashboard, indent=2) + "\n"
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "grafana-dashboards", "namespace": namespace(inventory_plan)},
        "data": dashboards or {"README": "No dashboards found."},
    }


def _prometheus_config(inventory_plan):
    return yaml.safe_dump({
        "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
        "scrape_configs": [{
            "job_name": "hazelcast-mc",
            "static_configs": [{"targets": [f"{management_center_name(inventory_plan)}:8080"]}],
            "metrics_path": "/metrics",
            "scrape_interval": "10s",
            "scrape_timeout": "5s",
        }],
    }, sort_keys=False)


def _external_exposure(inventory_plan):
    hz = inventory_plan.get("hazelcast") or {}
    external = hz.get("external") or {}
    exposure = external.get("discovery_service_type", "LoadBalancer")
    if exposure not in ("LoadBalancer", "NodePort"):
        exit_with_error("hazelcast.external.discovery_service_type must be LoadBalancer or NodePort")
    result = {
        "type": external.get("type", "Smart"),
        "discoveryServiceType": exposure,
    }
    if result["type"] == "Smart":
        default_member_access = "NodePortExternalIP" if exposure == "NodePort" else "LoadBalancer"
        result["memberAccess"] = external.get("member_access", default_member_access)
    return result


def _kubernetes_service_type(inventory_plan):
    return service_exposure(inventory_plan)


def _node_hosts(inventory_plan):
    live_pods = _hazelcast_pods(inventory_plan)
    if live_pods:
        return {
            pod["name"]: {
                "provider": "kubernetes",
                "pod": pod["name"],
                "namespace": namespace(inventory_plan),
                "private_ip": pod.get("ip") or pod["name"],
                "dc": pod.get("dc"),
                "node": pod.get("node"),
            }
            for pod in live_pods
        }
    return _planned_node_hosts(inventory_plan)


def _planned_node_hosts(inventory_plan):
    hz_name = hazelcast_resource_name(inventory_plan)
    size = int((inventory_plan.get("hazelcast") or {}).get("cluster_size", 1))
    dcs = _dc_plans(inventory_plan, size)
    assignments = []
    for dc in dcs:
        for _ in range(int(dc.get("members", 0))):
            assignments.append(dc.get("name"))
    while len(assignments) < size:
        assignments.append(dcs[0].get("name", "dc-a"))
    hosts = {}
    for index in range(size):
        pod = f"{hz_name}-{index}"
        dc = assignments[index]
        hosts[pod] = {
            "provider": "kubernetes",
            "pod": pod,
            "namespace": namespace(inventory_plan),
            "private_ip": pod,
            "dc": dc,
        }
    return hosts


def _dc_plans(inventory_plan, size=None):
    if size is None:
        size = int((inventory_plan.get("hazelcast") or {}).get("cluster_size", 1))
    return inventory_plan.get("dcs") or [{"name": "dc-a", "members": size}]


def _dc_names(inventory_plan):
    return [dc.get("name", "dc-a") for dc in _dc_plans(inventory_plan)]


def _loadgenerator_hosts(inventory_plan):
    name = _simulator_agents_name(inventory_plan)
    pods = _simulator_role_pods(inventory_plan, "loadgenerator")
    count = int((((inventory_plan.get("simulator") or {}).get("loadgenerators") or {}).get("count", 1)))
    pod_names = [pod["name"] for pod in pods] if pods else [f"{name}-{index}" for index in range(count)]
    result = {}
    for pod_name in pod_names:
        dns_name = f"{pod_name}.{name}.{namespace(inventory_plan)}.svc"
        result[dns_name] = {
            "provider": "kubernetes",
            "pod": pod_name,
            "namespace": namespace(inventory_plan),
            "context": kube_context(inventory_plan),
            "private_ip": dns_name,
        }
    return result


def _simulator_agents_name(inventory_plan):
    return f"{instance_name(inventory_plan)[:45]}-agents".strip("-")


def _simulator_service_account_name(inventory_plan):
    return f"{instance_name(inventory_plan)[:42]}-simulator".strip("-")


def _simulator_role_pods(inventory_plan, role):
    result = run_kubectl(inventory_plan, [
        "get", "pods", "-n", namespace(inventory_plan),
        "-l", f"{INSTANCE_LABEL}={instance_name(inventory_plan)},{ROLE_LABEL}={role}", "-o", "json",
    ], check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []
    pods = []
    for item in (json.loads(result.stdout).get("items") or []):
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        pods.append({
            "name": metadata.get("name"),
            "pod": metadata.get("name"),
            "ip": status.get("podIP"),
            "node": status.get("nodeName"),
            "ready": _pod_ready(item),
            "role": role,
        })
    return sorted(pods, key=_pod_name)


def prepare_kubernetes_agents(inventory_plan, agents, run_id):
    from simulator.hosts import public_ip
    from simulator.remote import copy_to_remote, remote_exec

    upload_dir = path.abspath("upload")
    reset = bool((((inventory_plan.get("simulator") or {}).get("loadgenerators") or {}).get(
        "reset_before_run", True
    )))
    for agent in agents:
        target = f"/opt/simulator/workers/{run_id}"
        # Results from every agent are copied into the same local run directory.
        # Keep the conventional *_dstat.csv suffix that the report loader
        # discovers, while making the name unique per Kubernetes pod.
        dstat_name = f"{agent.get('pod') or public_ip(agent)}_dstat.csv"
        command = f"rm -rf {target} && mkdir -p {target}" if reset else f"mkdir -p {target}"
        remote_exec(agent, command)
        if path.isdir(upload_dir):
            copy_to_remote(agent, upload_dir, target)
        remote_exec(
            agent,
            f"if command -v dstat >/dev/null 2>&1; then "
            f"nohup dstat --epoch -m --all -l --noheaders --nocolor "
            f"--output {target}/{dstat_name} 1 >/dev/null 2>&1 & fi",
        )


def run_kubernetes_coordinator(inventory_plan, coordinator_params, test_file, run_path, run_id):
    validate_inventory_plan(inventory_plan, require_license=False)
    simulator = inventory_plan.get("simulator") or {}
    coordinator = simulator.get("coordinator") or {}
    ns = namespace(inventory_plan)
    execution = re.sub(r"[^a-z0-9-]", "-", run_id.lower())[:20]
    pod_name = f"{instance_name(inventory_plan)[:35]}-coordinator-{execution}".strip("-")
    lock_name = f"{instance_name(inventory_plan)[:45]}-run-lock".strip("-")
    mkdir(GENERATED_DIR)

    lock = run_kubectl(inventory_plan, [
        "create", "configmap", lock_name, "-n", ns, f"--from-literal=run-id={run_id}",
    ], check=False, capture_output=True)
    if lock.returncode != 0:
        exit_with_error(
            f"Another Kubernetes Simulator run is active or lock [{lock_name}] is stale. "
            f"Inspect it with kubectl get configmap {lock_name} -n {ns}."
        )
    run_kubectl(inventory_plan, [
        "label", "configmap", lock_name, "-n", ns,
        f"{OWNER_LABEL}=true", f"{INSTANCE_LABEL}={instance_name(inventory_plan)}", "--overwrite",
    ])

    pod = _coordinator_pod_manifest(inventory_plan, pod_name)
    manifest_file = f"{GENERATED_DIR}/{pod_name}.yaml"
    with open(manifest_file, "w") as f:
        yaml.safe_dump(pod, f, sort_keys=False)
    os.chmod(manifest_file, 0o600)
    exit_code = 1
    retain = False
    try:
        run_kubectl(inventory_plan, ["apply", "-f", manifest_file])
        _wait_for_resource_condition(inventory_plan, "pod", pod_name, "ready", ns)
        run_kubectl(inventory_plan, ["exec", "-n", ns, pod_name, "--", "mkdir", "-p", "/workspace"])
        run_kubectl(inventory_plan, ["cp", "-n", ns, "inventory.yaml", f"{pod_name}:/workspace/inventory.yaml"])
        run_kubectl(inventory_plan, ["cp", "-n", ns, test_file, f"{pod_name}:/workspace/test.properties"])
        if path.isdir("upload"):
            run_kubectl(inventory_plan, ["cp", "-n", ns, "upload", f"{pod_name}:/workspace/upload"])

        command = ["exec", "-n", ns, pod_name, "--", "/opt/simulator/bin/hidden/coordinator"]
        for key, value in coordinator_params.items():
            command.extend(["--param", f"{key}={value}"])
        command.append("/workspace/test.properties")
        result = run_kubectl(inventory_plan, command, check=False)
        exit_code = result.returncode

        result_dir = f"{GENERATED_DIR}/coordinator-results-{execution}"
        if path.isdir(result_dir):
            shutil.rmtree(result_dir)
        copy_result = run_kubectl(inventory_plan, [
            "cp", "-n", ns, f"{pod_name}:/workspace/{run_path}", result_dir,
        ], check=False, capture_output=True)
        if copy_result.returncode == 0 and path.isdir(result_dir):
            os.makedirs(run_path, exist_ok=True)
            shutil.copytree(result_dir, run_path, dirs_exist_ok=True)
            shutil.rmtree(result_dir)
        else:
            warn(f"Could not retrieve coordinator results from pod [{pod_name}]")
        retain = exit_code != 0 and coordinator.get("retain_on_failure", False)
        chaos_events = f"{GENERATED_DIR}/chaos-events.jsonl"
        if path.isfile(chaos_events):
            os.makedirs(run_path, exist_ok=True)
            shutil.copy2(chaos_events, f"{run_path}/chaos-events.jsonl")
    finally:
        if not retain:
            run_kubectl(inventory_plan, ["delete", "pod", pod_name, "-n", ns, "--ignore-not-found=true"], check=False)
        run_kubectl(inventory_plan, ["delete", "configmap", lock_name, "-n", ns, "--ignore-not-found=true"], check=False)
        if path.exists(manifest_file):
            os.remove(manifest_file)
    return exit_code


def _coordinator_pod_manifest(inventory_plan, pod_name):
    simulator = inventory_plan.get("simulator") or {}
    coordinator = simulator.get("coordinator") or {}
    spec = {
        "serviceAccountName": _simulator_service_account_name(inventory_plan),
        "restartPolicy": "Never",
        "activeDeadlineSeconds": int(coordinator.get("active_deadline_seconds", 86400)),
        "containers": [{
            "name": "coordinator",
            "image": simulator["image"],
            "imagePullPolicy": simulator.get("image_pull_policy", "IfNotPresent"),
            "command": ["/bin/sh", "-lc", "trap : TERM INT; sleep infinity & wait"],
            "resources": coordinator.get("resources", {"requests": {"cpu": "1", "memory": "2Gi"}}),
            "env": [{"name": "SIMULATOR_IN_CLUSTER", "value": "true"}],
        }],
    }
    if simulator.get("image_pull_secrets"):
        spec["imagePullSecrets"] = [
            item if isinstance(item, dict) else {"name": item} for item in simulator["image_pull_secrets"]
        ]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace(inventory_plan),
            "labels": {
                OWNER_LABEL: "true",
                INSTANCE_LABEL: instance_name(inventory_plan),
                ROLE_LABEL: "coordinator",
            },
        },
        "spec": spec,
    }


def _cluster_service_endpoint(inventory_plan, service_name, port):
    host = f"{service_name}.{namespace(inventory_plan)}.svc"
    return {"host": host, "host_data": {"public_ip": host, "private_ip": host, "port": port}}


def _service_endpoint(inventory_plan, service_name, port):
    override = _endpoint_override(inventory_plan, service_name, port)
    if override:
        return override
    service = _service_json(inventory_plan, service_name)
    if not service:
        return None
    ingress = (((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [])
    host = ingress[0].get("ip") or ingress[0].get("hostname") if ingress else None
    service_type = (service.get("spec") or {}).get("type", "ClusterIP")
    endpoint_port = port
    if not host and service_type == "NodePort":
        host = _node_address(inventory_plan)
        endpoint_port = _service_node_port(service, port)
        if not endpoint_port:
            return None
    if not host and service_type == "ClusterIP":
        host = f"{service_name}.{namespace(inventory_plan)}.svc"
    if not host:
        return None
    return {
        "host": host,
        "host_data": {"public_ip": host, "private_ip": host, "port": endpoint_port},
    }


def _start_port_forward(inventory_plan, service_name, remote_port):
    key = (kube_context(inventory_plan), namespace(inventory_plan), service_name, remote_port)
    existing = _PORT_FORWARDS.get(key)
    if existing and existing[0].poll() is None:
        return existing[1]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    local_port = listener.getsockname()[1]
    listener.close()
    cmd = kubectl_base(inventory_plan) + [
        "-n", namespace(inventory_plan), "port-forward", f"service/{service_name}",
        f"{local_port}:{remote_port}",
    ]
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{local_port}"
    _PORT_FORWARDS[key] = (process, url)
    deadline = time.time() + 10
    while time.time() < deadline:
        if process.poll() is not None:
            exit_with_error(f"Could not port-forward Kubernetes service [{service_name}]")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", local_port)) == 0:
                return url
        time.sleep(0.1)
    process.terminate()
    exit_with_error(f"Timed out port-forwarding Kubernetes service [{service_name}]")


def _stop_port_forwards():
    for process, _url in _PORT_FORWARDS.values():
        if process.poll() is None:
            process.terminate()


atexit.register(_stop_port_forwards)


def _service_node_port(service, service_port):
    ports = (service.get("spec") or {}).get("ports") or []
    for item in ports:
        if item.get("port") == service_port or item.get("targetPort") == service_port:
            return item.get("nodePort")
    return ports[0].get("nodePort") if len(ports) == 1 else None


def _node_address(inventory_plan):
    configured = (inventory_plan.get("kubernetes") or {}).get("node_address")
    if configured:
        return configured
    result = run_kubectl(inventory_plan, ["get", "nodes", "-o", "json"], check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    items = json.loads(result.stdout).get("items") or []
    preferred_types = ["ExternalIP", "ExternalDNS"]
    if (inventory_plan.get("kubernetes") or {}).get("allow_node_internal_ip", False):
        preferred_types.append("InternalIP")
    for address_type in preferred_types:
        for item in items:
            for address in (item.get("status") or {}).get("addresses") or []:
                if address.get("type") == address_type and address.get("address"):
                    return address["address"]
    return None


def _endpoint_override(inventory_plan, service_name, default_port):
    endpoints = (inventory_plan.get("kubernetes") or {}).get("endpoints") or {}
    aliases = {
        hazelcast_resource_name(inventory_plan): "hazelcast",
        management_center_name(inventory_plan): "management_center",
    }
    value = endpoints.get(aliases.get(service_name, service_name))
    if not value:
        return None
    if isinstance(value, str):
        value = {"host": value}
    host = value.get("host")
    if not host:
        exit_with_error(f"Kubernetes endpoint override [{service_name}] requires host")
    port = int(value.get("port", default_port))
    scheme = value.get("scheme")
    result = {"host": host, "host_data": {"public_ip": host, "private_ip": host, "port": port}}
    if scheme in ("http", "https"):
        result["url"] = f"{scheme}://{host}" + (f":{port}" if port not in (80, 443) else "")
    return result


def _service_json(inventory_plan, service_name):
    result = run_kubectl(inventory_plan, ["get", "svc", service_name, "-n", namespace(inventory_plan), "-o", "json"],
                         check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return None
    return json.loads(result.stdout)


def _hazelcast_pods(inventory_plan):
    result = run_kubectl(inventory_plan, [
        "get", "pods", "-n", namespace(inventory_plan),
        "-l", f"app.kubernetes.io/instance={hazelcast_resource_name(inventory_plan)}",
        "-o", "json",
    ], check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []
    data = json.loads(result.stdout)
    node_labels = _node_labels(inventory_plan)
    pods = []
    for item in data.get("items", []):
        metadata = item.get("metadata") or {}
        status = item.get("status") or {}
        labels = metadata.get("labels") or {}
        node = (item.get("spec") or {}).get("nodeName")
        pods.append({
            "name": metadata.get("name"),
            "phase": status.get("phase"),
            "ip": status.get("podIP"),
            "dc": _pod_dc(inventory_plan, node_labels.get(node, {}), metadata.get("name")),
            "node": node,
            "ready": _pod_ready(item),
        })
    return pods


def _node_labels(inventory_plan):
    result = run_kubectl(
        inventory_plan,
        ["get", "nodes", "-o", "json"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout:
        return {}
    data = json.loads(result.stdout)
    return {
        (item.get("metadata") or {}).get("name"): (item.get("metadata") or {}).get("labels") or {}
        for item in data.get("items", [])
    }


def _pod_dc(inventory_plan, labels, pod_name=None):
    dcs = _dc_plans(inventory_plan)
    if _uses_synthetic_regions(inventory_plan):
        match = re.search(r"-(\d+)$", str(pod_name or ""))
        if match:
            ordinal = int(match.group(1))
            for dc in dcs:
                if ordinal in dc.get("pod_ordinals", []):
                    return dc["name"]
        return None
    if len(dcs) == 1 and not dcs[0].get("topology_value"):
        return dcs[0]["name"]
    value = labels.get(topology_key(inventory_plan))
    for dc in dcs:
        if dc.get("topology_value") == value:
            return dc["name"]
    return None


def _uses_synthetic_regions(inventory_plan):
    return any(dc.get("pod_ordinals") is not None for dc in _dc_plans(inventory_plan))


def _pod_ready(pod):
    statuses = ((pod.get("status") or {}).get("containerStatuses") or [])
    return bool(statuses) and all(item.get("ready") for item in statuses)


def _pod_name(pod):
    return pod.get("name") or pod.get("pod")


def _service_summary(inventory_plan):
    result = run_kubectl(inventory_plan, ["get", "svc", "-n", namespace(inventory_plan), "-o", "json"],
                         check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []
    data = json.loads(result.stdout)
    return [item.get("metadata", {}).get("name") for item in data.get("items", [])]


def _chaos_summary(inventory_plan):
    result = run_kubectl(inventory_plan, ["get", "networkchaos,podchaos", "-A", "-o", "json"],
                         check=False, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        return []
    data = json.loads(result.stdout)
    return [item.get("metadata", {}).get("name") for item in data.get("items", [])]


def _selected_pods(inventory_plan, hosts_pattern):
    inventory_hosts = _planned_node_hosts(inventory_plan)
    pods = _hazelcast_pods(inventory_plan) or list(inventory_hosts.values())
    if path.exists("inventory.yaml"):
        from inventory import load_hosts
        resolved_names = {host.get("public_ip") for host in load_hosts(host_pattern=hosts_pattern)}
        return [pod for pod in pods if _pod_name(pod) in resolved_names]

    requested = {
        item for token in hosts_pattern.split(":")
        for item in token.split(",")
        if item and not item.startswith("!")
    }
    excluded = {
        item[1:] for token in hosts_pattern.split(":")
        for item in token.split(",")
        if item.startswith("!")
    }
    if "all" in requested or "nodes" in requested:
        selected = pods
    else:
        selected = []
        for pod in pods:
            name = pod.get("name") or pod.get("pod")
            dc = pod.get("dc")
            if name in requested or dc in requested:
                selected.append(pod)
    return [
        pod for pod in selected
        if (pod.get("name") or pod.get("pod")) not in excluded and pod.get("dc") not in excluded
    ]


def _pod_chaos_manifest(inventory_plan, pods, duration_seconds):
    from simulator.chaos_kubernetes import builtin_pod_chaos
    return builtin_pod_chaos(inventory_plan, pods, duration_seconds)[0]


def _network_chaos_manifest(inventory_plan, partition_groups, duration_seconds):
    if len(partition_groups) != 2:
        exit_with_error("Kubernetes split-brain currently supports exactly two partitions.")
    from simulator.chaos_kubernetes import builtin_partition
    left, right = partition_groups
    return builtin_partition(inventory_plan, left, right, duration_seconds)


def _pod_selector(inventory_plan, pods):
    return {"pods": {namespace(inventory_plan): [_pod_name(pod) for pod in pods]}}


def _apply_temporary_chaos(inventory_plan, manifest, lapse_seconds, dry_run):
    if dry_run:
        return {"dry_run": True, "manifest": manifest}
    mkdir(GENERATED_DIR)
    file = f"{GENERATED_DIR}/{manifest['metadata']['name']}.yaml"
    with open(file, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False)
    os.chmod(file, 0o600)
    execution_id = None
    completed = False
    try:
        run_kubectl(inventory_plan, ["apply", "-f", file])
        from simulator.chaos_kubernetes import track_temporary_start
        execution_id = track_temporary_start(inventory_plan, manifest, file)
        time.sleep(max(lapse_seconds, 1))
        completed = True
    finally:
        run_kubectl(inventory_plan, ["delete", "-f", file, "--ignore-not-found=true"], check=False)
        if execution_id:
            from simulator.chaos_kubernetes import track_temporary_finish
            track_temporary_finish(inventory_plan, execution_id, "completed" if completed else "failed")
        if path.exists(file):
            os.remove(file)
    return {"dry_run": False, "manifest": manifest}


def _run_pod_chaos(inventory_plan, pods, lapse_seconds, start_spread_seconds, dry_run):
    manifests = []
    for pod in sorted(pods, key=_pod_name):
        manifest = _pod_chaos_manifest(inventory_plan, [pod], lapse_seconds)
        manifest["metadata"]["name"] = _chaos_name(inventory_plan, f"kill-{_pod_name(pod)}")
        manifests.append(manifest)
    if dry_run:
        return {
            "action": "chaos-mesh-pod-failure" if lapse_seconds else "chaos-mesh-pod-kill",
            "dry_run": True,
            "manifests": manifests,
            "pods": [_pod_name(pod) for pod in pods],
        }

    mkdir(GENERATED_DIR)
    files = {}
    execution_ids = {}
    events = []
    offsets = _pod_offsets(len(manifests), start_spread_seconds)
    hold_seconds = max(lapse_seconds, 1)
    for manifest, offset in zip(manifests, offsets):
        file = f"{GENERATED_DIR}/{manifest['metadata']['name']}.yaml"
        with open(file, "w") as f:
            yaml.safe_dump(manifest, f, sort_keys=False)
        os.chmod(file, 0o600)
        files[manifest["metadata"]["name"]] = file
        events.append((offset, "apply", manifest))
        events.append((offset + hold_seconds, "delete", manifest))

    started = time.time()
    applied = set()
    completed = False
    try:
        for offset, action, manifest in sorted(events, key=lambda event: (event[0], event[1] == "apply")):
            delay = started + offset - time.time()
            if delay > 0:
                time.sleep(delay)
            file = files[manifest["metadata"]["name"]]
            if action == "apply":
                run_kubectl(inventory_plan, ["apply", "-f", file])
                applied.add(file)
                from simulator.chaos_kubernetes import track_temporary_start
                execution_ids[file] = track_temporary_start(inventory_plan, manifest, file)
            elif file in applied:
                run_kubectl(inventory_plan, ["delete", "-f", file, "--ignore-not-found=true"], check=False)
                applied.discard(file)
        completed = True
    finally:
        for file in applied:
            run_kubectl(inventory_plan, ["delete", "-f", file, "--ignore-not-found=true"], check=False)
        for file in files.values():
            if file in execution_ids:
                from simulator.chaos_kubernetes import track_temporary_finish
                track_temporary_finish(
                    inventory_plan, execution_ids[file], "completed" if completed else "failed"
                )
            if path.exists(file):
                os.remove(file)

    for pod in pods:
        _wait_for_named_pod(inventory_plan, _pod_name(pod))
    return {"action": "chaos-mesh", "dry_run": False, "pods": [_pod_name(pod) for pod in pods]}


def _pod_offsets(count, spread_seconds):
    if count <= 1:
        return [0] * count
    return [int(index * spread_seconds / (count - 1)) for index in range(count)]


def _pod_schedule(pods, spread_seconds):
    ordered = sorted(pods, key=_pod_name)
    offsets = _pod_offsets(len(ordered), spread_seconds)
    previous = 0
    result = []
    for pod, offset in zip(ordered, offsets):
        result.append((pod, offset - previous))
        previous = offset
    return result


def _wait_for_named_pod(inventory_plan, pod_name):
    _wait_for_resource_condition(inventory_plan, "pod", pod_name, "ready", namespace(inventory_plan))


def _wait_for_resource_condition(inventory_plan, resource_type, name, condition, resource_namespace):
    timeout = int((inventory_plan.get("kubernetes") or {}).get("wait_timeout_seconds", 600))
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_kubectl(
            inventory_plan,
            ["get", resource_type, name, "-n", resource_namespace],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            remaining = max(1, int(deadline - time.time()))
            wait_result = run_kubectl(inventory_plan, [
                "wait", f"--for=condition={condition}", f"{resource_type}/{name}",
                "-n", resource_namespace, f"--timeout={remaining}s",
            ], check=False, capture_output=True)
            if wait_result.returncode == 0:
                return
        time.sleep(2)
    exit_with_error(
        f"Timed out waiting for Kubernetes resource [{resource_type}/{name}] to satisfy condition [{condition}]"
    )


def _wait_for_selector_condition(inventory_plan, resource_type, selector, condition, resource_namespace):
    timeout = int((inventory_plan.get("kubernetes") or {}).get("wait_timeout_seconds", 600))
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_kubectl(
            inventory_plan,
            ["get", resource_type, "-l", selector, "-n", resource_namespace, "-o", "json"],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0 and result.stdout and (json.loads(result.stdout).get("items") or []):
            remaining = max(1, int(deadline - time.time()))
            wait_result = run_kubectl(inventory_plan, [
                "wait", f"--for=condition={condition}", resource_type, "-l", selector,
                "-n", resource_namespace, f"--timeout={remaining}s",
            ], check=False, capture_output=True)
            if wait_result.returncode == 0:
                return
        time.sleep(2)
    exit_with_error(
        f"Timed out waiting for Kubernetes resources [{resource_type} -l {selector}] "
        f"to satisfy condition [{condition}]"
    )


def _chaos_namespace(inventory_plan):
    return (inventory_plan.get("chaosmesh") or {}).get("namespace", "chaos-mesh")


def _chaos_name(inventory_plan, action):
    value = f"simulator-{instance_name(inventory_plan)}-{action}"
    return re.sub(r"[^a-z0-9.-]+", "-", value.lower()).strip("-.")[:63]


def _parse_partitions(partitions):
    result = []
    for group in partitions.split("/"):
        values = [item.strip() for item in group.split(",") if item.strip()]
        if values:
            result.append(values)
    if len(result) != 2:
        exit_with_error("--partitions must contain exactly two groups separated by /")
    return result


def _mc_enabled(inventory_plan):
    return observability_enabled(inventory_plan) or bool((inventory_plan.get("mc") or {}).get("enabled", True))


def _pdb_enabled(inventory_plan):
    return bool(((inventory_plan.get("hazelcast") or {}).get("pdb") or {}).get("enabled", True))


def _chaosmesh_enabled(inventory_plan):
    return bool((inventory_plan.get("chaosmesh") or {}).get("enabled", False))


def _chaosmesh_install(inventory_plan):
    return bool((inventory_plan.get("chaosmesh") or {}).get("install", False))


def _required(data, key, name):
    value = data.get(key)
    if value in (None, ""):
        exit_with_error(f"Missing required setting [{name}]")
    return value


def _positive_int(value, name):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        exit_with_error(f"{name} must be a positive integer")
    if parsed < 1:
        exit_with_error(f"{name} must be a positive integer")
    return parsed


def _validate_input_file(filename, name, allow_empty):
    if not filename:
        return
    expanded = os.path.expanduser(filename)
    if not path.isfile(expanded):
        exit_with_error(f"Configured file [{name}] does not exist: {expanded}")
    if not allow_empty and path.getsize(expanded) == 0:
        exit_with_error(f"Configured file [{name}] is empty: {expanded}")


def _require_tool(tool):
    if not shutil.which(tool):
        exit_with_error(f"Required tool [{tool}] is not installed or not on PATH.")


def _run_cmd(cmd, check=True, capture_output=False):
    info(" ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=capture_output)
    if check and result.returncode != 0:
        exit_with_error(f"Command failed, exitcode={result.returncode}, command=[{' '.join(cmd)}]")
    return result
