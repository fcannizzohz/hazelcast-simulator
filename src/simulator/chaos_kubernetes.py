import json
import os
import re
import time
import uuid
from copy import deepcopy
from os import path
from urllib.request import Request, urlopen

import yaml

from simulator.log import warn
from simulator.util import exit_with_error, mkdir


BUILTIN_PROFILES = (
    "_builtin.kill-members",
    "_builtin.split-brain",
    "_builtin.inject-latency",
)
CHAOS_DIR = ".simulator-k8s/chaos"
CHAOS_EVENTS = ".simulator-k8s/chaos-events.jsonl"
VALID_SCOPES = ("workload", "cluster", "cloud")


def validate_chaos_configuration(inventory_plan):
    chaos = inventory_plan.get("chaosmesh") or {}
    profiles = _profiles(inventory_plan)
    if not isinstance(profiles, dict):
        exit_with_error("chaosmesh.profiles must be a mapping")
    if profiles and not chaos.get("enabled", False):
        exit_with_error("chaosmesh.profiles requires chaosmesh.enabled: true")
    _duration_seconds(chaos.get("default_duration", "5m"), "chaosmesh.default_duration")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name or name.startswith("_builtin"):
            exit_with_error(f"Chaos profile name [{name}] is invalid or reserved")
        if not isinstance(profile, dict):
            exit_with_error(f"Chaos profile [{name}] must be a mapping")
        has_manifest = bool(profile.get("manifest"))
        has_structured = bool(profile.get("kind") or profile.get("spec"))
        if has_manifest == has_structured:
            exit_with_error(f"Chaos profile [{name}] must define either manifest or kind/spec")
        if has_structured and (not profile.get("kind") or not isinstance(profile.get("spec"), dict)):
            exit_with_error(f"Chaos profile [{name}] requires kind and a mapping spec")
        scope = profile.get("scope", "workload")
        if scope not in VALID_SCOPES:
            exit_with_error(f"Chaos profile [{name}] has unsupported scope [{scope}]")
        if profile.get("duration") is not None:
            _duration_seconds(profile["duration"], f"chaosmesh.profiles.{name}.duration")
        target = profile.get("target")
        if target is not None and not isinstance(target, dict):
            exit_with_error(f"Chaos profile [{name}].target must be a mapping")


def chaos_list(inventory_plan):
    profiles = [{"name": name, "builtin": True} for name in BUILTIN_PROFILES]
    for name, profile in sorted(_profiles(inventory_plan).items()):
        profiles.append({
            "name": name,
            "builtin": False,
            "kind": profile.get("kind") or "manifest",
            "scope": profile.get("scope", "workload"),
            "persistent": bool(profile.get("persistent", False)),
        })
    return {"profiles": profiles, "executions": chaos_status(inventory_plan)}


def chaos_render(inventory_plan, profile_name, duration=None, allow_elevated=False, execution_id=None):
    from simulator.inventory_kubernetes import validate_inventory_plan

    validate_inventory_plan(inventory_plan, require_license=False)
    profile = _user_profile(inventory_plan, profile_name)
    execution_id = execution_id or uuid.uuid4().hex[:12]
    scope = profile.get("scope", "workload")
    _require_scope(inventory_plan, scope, allow_elevated)
    docs = _profile_documents(inventory_plan, profile_name, profile, execution_id, duration)
    for doc in docs:
        _validate_document(inventory_plan, doc, scope)
    return {
        "execution_id": execution_id,
        "profile": profile_name,
        "scope": scope,
        "persistent": bool(profile.get("persistent", False)),
        "manifests": docs,
    }


def chaos_run(inventory_plan, profile_name, duration=None, detach=False, dry_run=False, allow_elevated=False):
    from simulator.inventory_kubernetes import _chaosmesh_enabled, _verify_chaosmesh, run_kubectl

    if not _chaosmesh_enabled(inventory_plan):
        exit_with_error("Chaos profiles require chaosmesh.enabled: true")
    rendered = chaos_render(inventory_plan, profile_name, duration, allow_elevated)
    docs = rendered["manifests"]
    kinds = {doc["kind"] for doc in docs}
    if rendered["persistent"] and not detach:
        exit_with_error("Persistent chaos profiles require --detach")
    if "Schedule" in kinds and not detach:
        exit_with_error("Chaos Mesh Schedule profiles require --detach")
    if dry_run:
        return {**rendered, "dry_run": True}

    _verify_chaosmesh(inventory_plan, kinds=kinds)
    mkdir(CHAOS_DIR)
    execution_id = rendered["execution_id"]
    manifest_file = f"{CHAOS_DIR}/{execution_id}.yaml"
    with open(manifest_file, "w") as f:
        yaml.safe_dump_all(docs, f, sort_keys=False)
    os.chmod(manifest_file, 0o600)
    record = {
        "execution_id": execution_id,
        "profile": profile_name,
        "scope": rendered["scope"],
        "persistent": rendered["persistent"],
        "manifest_file": manifest_file,
        "resources": [_resource_ref(doc) for doc in docs],
        "status": "active",
        "started_at": int(time.time()),
    }
    _write_record(record)
    _event(record, "start", inventory_plan)
    try:
        run_kubectl(inventory_plan, ["apply", "-f", manifest_file])
        _event(record, "injected", inventory_plan)
        if detach:
            return {**rendered, "dry_run": False, "status": "active"}
        if "Workflow" in kinds:
            _wait_for_workflows(inventory_plan, docs)
        else:
            hold = _execution_duration(inventory_plan, profile_name, duration)
            time.sleep(hold)
    except BaseException:
        record["status"] = "failed"
        _write_record(record)
        _event(record, "failed", inventory_plan)
        _delete_documents(inventory_plan, docs)
        raise
    _delete_documents(inventory_plan, docs)
    record["status"] = "completed"
    record["finished_at"] = int(time.time())
    _write_record(record)
    _event(record, "recovered", inventory_plan)
    return {**rendered, "dry_run": False, "status": "completed"}


def chaos_status(inventory_plan, execution_id=None):
    records = []
    for record_file in _record_files(execution_id):
        with open(record_file) as f:
            record = yaml.safe_load(f) or {}
        if record.get("status") == "active":
            active = []
            for resource in record.get("resources") or []:
                active.append(_resource_exists(inventory_plan, resource))
            if active and not any(active):
                record["status"] = "absent"
        records.append(record)
    return records


def chaos_stop(inventory_plan, execution_id, dry_run=False):
    record_file = _record_path(execution_id)
    if not path.exists(record_file):
        exit_with_error(f"Unknown chaos execution [{execution_id}]")
    with open(record_file) as f:
        record = yaml.safe_load(f) or {}
    manifest_file = record.get("manifest_file")
    if not manifest_file or not path.exists(manifest_file):
        exit_with_error(f"Chaos execution [{execution_id}] has no recoverable owned manifest")
    with open(manifest_file) as f:
        docs = [doc for doc in yaml.safe_load_all(f) if doc]
    if dry_run:
        return {"execution_id": execution_id, "dry_run": True, "resources": record.get("resources") or []}
    _delete_documents(inventory_plan, docs)
    record["status"] = "stopped"
    record["finished_at"] = int(time.time())
    _write_record(record)
    _event(record, "stopped", inventory_plan)
    return {"execution_id": execution_id, "dry_run": False, "status": "stopped"}


def cleanup_owned_chaos(inventory_plan):
    from simulator.inventory_kubernetes import run_kubectl

    for record in chaos_status(inventory_plan):
        if record.get("status") == "active":
            chaos_stop(inventory_plan, record["execution_id"])
    resources = run_kubectl(inventory_plan, [
        "api-resources", "--api-group=chaos-mesh.org", "--namespaced=true", "-o", "name",
    ], check=False, capture_output=True)
    if resources.returncode != 0:
        return
    selector = (
        f"simulator.hazelcast.com/managed=true,"
        f"simulator.hazelcast.com/instance={_instance_name(inventory_plan)}"
    )
    for resource_type in (resources.stdout or "").splitlines():
        if resource_type.strip():
            run_kubectl(inventory_plan, [
                "delete", resource_type.strip(), "-n", _chaos_namespace(inventory_plan),
                "-l", selector, "--ignore-not-found=true",
            ], check=False)


def builtin_pod_chaos(inventory_plan, pods, duration_seconds, action_name="kill-members"):
    action = "pod-failure" if duration_seconds > 0 else "pod-kill"
    profile = {
        "kind": "PodChaos",
        "targets": ",".join(_pod_name(pod) for pod in pods),
        "duration": f"{max(duration_seconds, 1)}s",
        "scope": "workload",
        "spec": {"action": action, "mode": "one" if len(pods) == 1 else "all"},
    }
    return _render_inline(inventory_plan, f"_builtin.{action_name}", profile)


def builtin_partition(inventory_plan, left, right, duration_seconds):
    profile = {
        "kind": "NetworkChaos",
        "targets": ",".join(_pod_name(pod) for pod in left),
        "target": {"targets": ",".join(_pod_name(pod) for pod in right), "mode": "all"},
        "duration": f"{max(duration_seconds, 1)}s",
        "scope": "workload",
        "spec": {"action": "partition", "mode": "all", "direction": "both"},
    }
    return _render_inline(inventory_plan, "_builtin.split-brain", profile)[0]


def builtin_latency(inventory_plan, hosts, target_hosts, latency, jitter=0, correlation=0, duration="5m"):
    delay = {"latency": f"{latency}ms"}
    if jitter:
        delay["jitter"] = f"{jitter}ms"
    if correlation:
        delay["correlation"] = str(correlation)
    profile = {
        "kind": "NetworkChaos",
        "targets": hosts,
        "target": {"targets": target_hosts, "mode": "all"} if target_hosts else None,
        "duration": duration,
        "scope": "workload",
        "spec": {"action": "delay", "mode": "all", "direction": "both", "delay": delay},
    }
    if profile["target"] is None:
        del profile["target"]
    return _render_inline(inventory_plan, "_builtin.inject-latency", profile)[0]


def inject_latency(inventory_plan, hosts, target_hosts, latency, jitter, correlation, duration, dry_run):
    from simulator.inventory_kubernetes import (
        _chaosmesh_enabled, _verify_chaosmesh, run_kubectl, validate_inventory_plan,
    )

    validate_inventory_plan(inventory_plan, require_license=False)
    if not _chaosmesh_enabled(inventory_plan):
        exit_with_error("Kubernetes latency injection requires chaosmesh.enabled: true")

    execution_id = uuid.uuid4().hex[:12]
    manifest = builtin_latency(
        inventory_plan, hosts, target_hosts, latency, jitter, correlation, duration
    )
    manifest["metadata"]["labels"]["simulator.hazelcast.com/chaos-execution"] = execution_id
    manifest["metadata"]["name"] = _resource_name(
        inventory_plan, "_builtin.inject-latency", execution_id, ""
    )
    if dry_run:
        return {"execution_id": execution_id, "dry_run": True, "manifest": manifest}
    _verify_chaosmesh(inventory_plan, kinds={"NetworkChaos"})
    mkdir(CHAOS_DIR)
    manifest_file = f"{CHAOS_DIR}/{execution_id}.yaml"
    with open(manifest_file, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False)
    os.chmod(manifest_file, 0o600)
    record = {
        "execution_id": execution_id,
        "profile": "_builtin.inject-latency",
        "scope": "workload",
        "persistent": False,
        "manifest_file": manifest_file,
        "resources": [_resource_ref(manifest)],
        "status": "active",
        "started_at": int(time.time()),
    }
    _write_record(record)
    run_kubectl(inventory_plan, ["apply", "-f", manifest_file])
    _event(record, "injected", inventory_plan)
    return {"execution_id": execution_id, "dry_run": False, "status": "active", "manifest": manifest}


def clear_latencies(inventory_plan, execution_id=None, dry_run=False):
    records = [
        record for record in chaos_status(inventory_plan, execution_id)
        if record.get("profile") == "_builtin.inject-latency" and record.get("status") == "active"
    ]
    if not records:
        return {"dry_run": dry_run, "stopped": []}
    results = [chaos_stop(inventory_plan, record["execution_id"], dry_run) for record in records]
    return {"dry_run": dry_run, "stopped": results}


def apply_builtin_temporary(inventory_plan, docs, duration_seconds, dry_run):
    from simulator.inventory_kubernetes import _apply_temporary_chaos

    if len(docs) != 1:
        exit_with_error("Built-in temporary chaos requires exactly one manifest")
    return _apply_temporary_chaos(inventory_plan, docs[0], duration_seconds, dry_run)


def track_temporary_start(inventory_plan, manifest, manifest_file):
    labels = (manifest.get("metadata") or {}).get("labels") or {}
    execution_id = labels.get("simulator.hazelcast.com/chaos-execution") or uuid.uuid4().hex[:12]
    record = {
        "execution_id": execution_id,
        "profile": labels.get("simulator.hazelcast.com/chaos-profile", "_builtin.control"),
        "scope": "workload",
        "persistent": False,
        "manifest_file": manifest_file,
        "resources": [_resource_ref(manifest)],
        "status": "active",
        "started_at": int(time.time()),
    }
    _write_record(record)
    _event(record, "injected", inventory_plan)
    return execution_id


def track_temporary_finish(inventory_plan, execution_id, status="completed"):
    filename = _record_path(execution_id)
    if not path.exists(filename):
        return
    with open(filename) as f:
        record = yaml.safe_load(f) or {}
    record["status"] = status
    record["finished_at"] = int(time.time())
    _write_record(record)
    _event(record, "recovered" if status == "completed" else status, inventory_plan)


def _render_inline(inventory_plan, name, profile):
    execution_id = uuid.uuid4().hex[:12]
    return _profile_documents(inventory_plan, name, profile, execution_id, None)


def _user_profile(inventory_plan, name):
    if name in BUILTIN_PROFILES:
        exit_with_error(f"Built-in profile [{name}] is invoked through its existing control command")
    profile = _profiles(inventory_plan).get(name)
    if profile is None:
        exit_with_error(f"Unknown chaos profile [{name}]")
    return deepcopy(profile)


def _profiles(inventory_plan):
    chaos = inventory_plan.get("chaosmesh") or {}
    inline = chaos.get("profiles") or {}
    filename = chaos.get("profiles_file")
    if not filename:
        return inline
    if inline:
        exit_with_error("Use either chaosmesh.profiles or chaosmesh.profiles_file, not both")
    filename = os.path.expanduser(filename)
    if not path.isfile(filename):
        exit_with_error(f"chaosmesh.profiles_file [{filename}] does not exist")
    try:
        with open(filename) as f:
            profiles = yaml.safe_load(f) or {}
    except yaml.YAMLError as error:
        exit_with_error(f"chaosmesh.profiles_file [{filename}] is not valid YAML: {error}")
    if not isinstance(profiles, dict):
        exit_with_error("chaosmesh.profiles_file must contain a profile mapping")
    return profiles


def _profile_documents(inventory_plan, name, profile, execution_id, duration_override):
    if profile.get("manifest"):
        filename = os.path.expanduser(profile["manifest"])
        if not path.isfile(filename):
            exit_with_error(f"Chaos profile [{name}] manifest [{filename}] does not exist")
        with open(filename) as f:
            docs = [deepcopy(doc) for doc in yaml.safe_load_all(f) if doc]
        if not docs:
            exit_with_error(f"Chaos profile [{name}] manifest [{filename}] is empty")
        docs = [_expand_placeholders(inventory_plan, doc) for doc in docs]
        if duration_override:
            for doc in docs:
                if doc.get("kind") not in ("Workflow", "Schedule"):
                    doc.setdefault("spec", {})["duration"] = str(duration_override)
    else:
        spec = deepcopy(profile["spec"])
        if profile.get("mode") is not None:
            spec["mode"] = profile["mode"]
        if profile.get("targets"):
            spec["selector"] = _selector(inventory_plan, profile["targets"])
        if profile.get("target"):
            target = deepcopy(profile["target"])
            if not target.get("targets"):
                exit_with_error(f"Chaos profile [{name}].target requires targets")
            target_selector = _selector(inventory_plan, target.pop("targets"))
            spec["target"] = {**target, "selector": target_selector}
        duration = duration_override or profile.get("duration")
        if duration is None and profile.get("kind") not in ("Workflow", "Schedule"):
            duration = (inventory_plan.get("chaosmesh") or {}).get("default_duration", "5m")
        if duration is not None:
            spec["duration"] = str(duration)
        docs = [{"apiVersion": "chaos-mesh.org/v1alpha1", "kind": profile["kind"], "spec": spec}]

    for index, doc in enumerate(docs):
        metadata = doc.setdefault("metadata", {})
        metadata["namespace"] = _chaos_namespace(inventory_plan)
        suffix = f"-{index}" if len(docs) > 1 else ""
        metadata["name"] = _resource_name(inventory_plan, name, execution_id, suffix)
        labels = metadata.setdefault("labels", {})
        labels.update({
            "simulator.hazelcast.com/managed": "true",
            "simulator.hazelcast.com/instance": _instance_name(inventory_plan),
            "simulator.hazelcast.com/chaos-execution": execution_id,
            "simulator.hazelcast.com/chaos-profile": _label_value(name),
        })
    return docs


def _selector(inventory_plan, expression):
    pods = _resolve_targets(inventory_plan, expression)
    if not pods:
        exit_with_error(f"Chaos target [{expression}] resolved to no current pods")
    return {"pods": {_workload_namespace(inventory_plan): [_pod_name(pod) for pod in pods]}}


def _resolve_targets(inventory_plan, expression):
    from simulator.inventory_kubernetes import _selected_pods, _simulator_role_pods

    included = []
    excluded = set()
    tokens = [item.strip() for item in re.split(r"[:,]", str(expression)) if item.strip()]
    for token in tokens:
        negate = token.startswith("!")
        value = token[1:] if negate else token
        if value in ("loadgenerators", "simulator_agents"):
            selected = _simulator_role_pods(inventory_plan, "loadgenerator")
        elif value == "coordinator":
            selected = _simulator_role_pods(inventory_plan, "coordinator")
        elif value == "simulator":
            selected = (
                _simulator_role_pods(inventory_plan, "loadgenerator")
                + _simulator_role_pods(inventory_plan, "coordinator")
            )
        else:
            selected = _selected_pods(inventory_plan, value)
        if negate:
            excluded.update(_pod_name(pod) for pod in selected)
        else:
            included.extend(selected)
    unique = {_pod_name(pod): pod for pod in included if _pod_name(pod) not in excluded}
    return [unique[name] for name in sorted(unique)]


def _validate_document(inventory_plan, doc, scope):
    api_version = str(doc.get("apiVersion", ""))
    if not api_version.startswith("chaos-mesh.org/"):
        exit_with_error(f"Chaos manifest API group [{api_version}] is not allowed")
    if not doc.get("kind") or not isinstance(doc.get("spec"), dict):
        exit_with_error("Chaos manifests require kind and a mapping spec")
    if scope != "workload":
        return
    if doc["kind"] in ("AWSChaos", "AzureChaos", "GCPChaos", "PhysicalMachineChaos"):
        exit_with_error(f"Chaos kind [{doc['kind']}] requires cloud scope")
    _validate_workload_tree(doc["spec"], _workload_namespace(inventory_plan))


def _validate_workload_tree(value, workload_namespace):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "externalTargets" and item:
                exit_with_error("externalTargets requires elevated cluster scope")
            if key in ("namespaceSelectors", "physicalMachineSelector") and item:
                exit_with_error(f"{key} requires elevated cluster scope")
            if key == "namespaces" and any(ns != workload_namespace for ns in (item or [])):
                exit_with_error("Workload chaos selectors cannot target another namespace")
            if key == "pods" and isinstance(item, dict) and any(ns != workload_namespace for ns in item):
                exit_with_error("Workload chaos pod selectors cannot target another namespace")
            _validate_workload_tree(item, workload_namespace)
    elif isinstance(value, list):
        for item in value:
            _validate_workload_tree(item, workload_namespace)


def _require_scope(inventory_plan, scope, allow_elevated):
    if scope == "workload":
        return
    configured = bool((inventory_plan.get("chaosmesh") or {}).get("allow_elevated_scope", False))
    if not configured or not allow_elevated:
        exit_with_error(
            f"Chaos scope [{scope}] requires chaosmesh.allow_elevated_scope: true and --allow-elevated"
        )


def _wait_for_workflows(inventory_plan, docs):
    from simulator.inventory_kubernetes import run_kubectl

    timeout = str((inventory_plan.get("chaosmesh") or {}).get("workflow_timeout", "1h"))
    for doc in docs:
        if doc.get("kind") == "Workflow":
            result = run_kubectl(inventory_plan, [
                "wait", "workflow", doc["metadata"]["name"], "-n", _chaos_namespace(inventory_plan),
                "--for=jsonpath={.status.endTime}", f"--timeout={timeout}",
            ], check=False)
            if result.returncode != 0:
                exit_with_error(f"Chaos workflow [{doc['metadata']['name']}] did not complete")


def _execution_duration(inventory_plan, profile_name, override):
    profile = _user_profile(inventory_plan, profile_name)
    value = override or profile.get("duration") or (inventory_plan.get("chaosmesh") or {}).get("default_duration", "5m")
    return _duration_seconds(value, f"chaos profile [{profile_name}] duration")


def _duration_seconds(value, name):
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)", str(value or ""))
    if not match:
        exit_with_error(f"{name} must be a positive duration such as 500ms, 30s, 5m, or 1h")
    amount = float(match.group(1))
    factor = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if amount <= 0:
        exit_with_error(f"{name} must be positive")
    return amount * factor


def _delete_documents(inventory_plan, docs):
    from simulator.inventory_kubernetes import run_kubectl

    for doc in reversed(docs):
        run_kubectl(inventory_plan, [
            "delete", doc["kind"].lower(), doc["metadata"]["name"],
            "-n", doc["metadata"]["namespace"], "--ignore-not-found=true",
        ], check=False)


def _resource_exists(inventory_plan, resource):
    from simulator.inventory_kubernetes import run_kubectl

    result = run_kubectl(inventory_plan, [
        "get", resource["kind"].lower(), resource["name"], "-n", resource["namespace"],
    ], check=False, capture_output=True)
    return result.returncode == 0


def _resource_ref(doc):
    return {
        "kind": doc["kind"],
        "name": doc["metadata"]["name"],
        "namespace": doc["metadata"]["namespace"],
    }


def _record_files(execution_id):
    if execution_id:
        filename = _record_path(execution_id)
        return [filename] if path.exists(filename) else []
    if not path.isdir(CHAOS_DIR):
        return []
    return sorted(
        f"{CHAOS_DIR}/{name}" for name in os.listdir(CHAOS_DIR)
        if name.endswith(".record.yaml")
    )


def _record_path(execution_id):
    return f"{CHAOS_DIR}/{execution_id}.record.yaml"


def _write_record(record):
    mkdir(CHAOS_DIR)
    filename = _record_path(record["execution_id"])
    temporary = f"{filename}.tmp-{os.getpid()}"
    with open(temporary, "w") as f:
        yaml.safe_dump(record, f, sort_keys=False)
    os.chmod(temporary, 0o600)
    os.replace(temporary, filename)


def _event(record, state, inventory_plan=None):
    mkdir(path.dirname(CHAOS_EVENTS))
    event = {
        "timestamp": int(time.time()),
        "execution_id": record["execution_id"],
        "profile": record["profile"],
        "state": state,
        "resources": record.get("resources") or [],
    }
    with open(CHAOS_EVENTS, "a") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    if inventory_plan is not None:
        _publish_grafana_annotation(inventory_plan, event)


def _publish_grafana_annotation(inventory_plan, event):
    from simulator.inventory_kubernetes import _service_endpoint, _start_port_forward, observability_enabled

    if not observability_enabled(inventory_plan):
        return
    try:
        endpoint = _service_endpoint(inventory_plan, "grafana", 3000)
        if not endpoint:
            return
        if endpoint["host"].endswith(".svc"):
            base_url = _start_port_forward(inventory_plan, "grafana", 3000)
        elif endpoint.get("url"):
            base_url = endpoint["url"]
        else:
            base_url = f"http://{endpoint['host']}:{endpoint['host_data'].get('port', 3000)}"
        payload = json.dumps({
            "time": event["timestamp"] * 1000,
            "tags": ["simulator", "chaos", event["profile"], event["state"]],
            "text": f"Chaos {event['profile']} {event['state']} ({event['execution_id']})",
        }).encode("utf-8")
        request = Request(
            f"{base_url.rstrip('/')}/api/annotations", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=5):
            pass
    except Exception as error:
        warn(f"Could not publish Grafana chaos annotation: {error}")


def _resource_name(inventory_plan, profile, execution_id, suffix):
    value = f"{_instance_name(inventory_plan)}-{profile}-{execution_id}{suffix}".lower()
    value = re.sub(r"[^a-z0-9-]", "-", value).strip("-")
    return value[:63].rstrip("-")


def _expand_placeholders(inventory_plan, value):
    replacements = {
        "${WORKLOAD_NAMESPACE}": _workload_namespace(inventory_plan),
        "${CHAOS_NAMESPACE}": _chaos_namespace(inventory_plan),
        "${SIMULATOR_INSTANCE}": _instance_name(inventory_plan),
        "${HAZELCAST_RESOURCE}": (inventory_plan.get("hazelcast") or {}).get("name", "hazelcast"),
    }
    if isinstance(value, dict):
        return {key: _expand_placeholders(inventory_plan, item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_placeholders(inventory_plan, item) for item in value]
    if isinstance(value, str):
        for source, target in replacements.items():
            value = value.replace(source, target)
    return value


def _label_value(value):
    result = re.sub(r"[^A-Za-z0-9_.-]", "-", value).strip("-_.")
    return result[:63] or "profile"


def _pod_name(pod):
    return pod.get("name") or pod.get("pod")


def _chaos_namespace(inventory_plan):
    return (inventory_plan.get("chaosmesh") or {}).get("namespace", "chaos-mesh")


def _workload_namespace(inventory_plan):
    return (inventory_plan.get("kubernetes") or {}).get("namespace", "default")


def _instance_name(inventory_plan):
    from simulator.inventory_kubernetes import instance_name
    return instance_name(inventory_plan)
