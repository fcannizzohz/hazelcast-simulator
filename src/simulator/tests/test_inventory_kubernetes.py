import json
import os
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import yaml

from simulator.inventory_kubernetes import (
    GENERATED_DIR,
    GENERATED_MANIFEST,
    INSTANCE_LABEL,
    OWNER_LABEL,
    _delete_rendered_manifests,
    _gke_apply,
    _gke_destroy,
    _network_chaos_manifest,
    _pod_dc,
    _redacted_command,
    _service_endpoint,
    _simulator_runtime_manifests,
    _verify_resource_ownership,
    _verify_license_secret,
    _verify_observability,
    _grafana_dashboard_configmap,
    _verify_dc_distribution,
    _wait_for_resource_condition,
    control_kill_members,
    control_split_brain,
    generate_inventory,
    kubernetes_provider,
    kubernetes_destroy,
    management_center_endpoint,
    render_manifests,
    run_kubernetes_coordinator,
    validate_inventory_plan,
)


def plan(**overrides):
    result = {
        "provisioner": "kubernetes",
        "kubernetes": {
            "provider": "static",
            "instance": "test-k8s",
            "namespace": "simulator",
            "service_type": "LoadBalancer",
            "topology_key": "topology.kubernetes.io/zone",
        },
        "hazelcast": {
            "name": "workers",
            "cluster_name": "workers",
            "cluster_size": 4,
            "version": "5.7.0",
        },
        "simulator": {
            "image": "registry.example.test/hazelcast-simulator:test",
            "loadgenerators": {"count": 2},
        },
        "mc": {"enabled": True, "name": "management-center"},
        "dcs": [
            {"name": "dc-a", "members": 2, "topology_value": "zone-a"},
            {"name": "dc-b", "members": 2, "topology_value": "zone-b"},
        ],
    }
    result.update(overrides)
    return result


def load_balancer_service(host):
    return {
        "spec": {"type": "LoadBalancer"},
        "status": {"loadBalancer": {"ingress": [{"ip": host}]}},
    }


class TestInventoryKubernetes(unittest.TestCase):

    @patch("simulator.inventory_kubernetes._observability_http")
    def test_observability_verification_requires_healthy_management_center_scrape(self, http):
        http.side_effect = [b"# HELP hazelcast_metric 1\n", {"database": "ok"}, {
            "data": {"activeTargets": [{"labels": {"job": "hazelcast-mc"}, "health": "up"}]}
        }]
        _verify_observability(plan(observability={"enabled": True}))
        self.assertEqual(3, http.call_count)

    def test_grafana_dashboard_configmap_includes_reportable_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = os.path.join(directory, "runs", "smoke", "16-07-2026_08-23-37", "report")
            os.makedirs(root)
            with open(os.path.join(root, "report.csv"), "w") as file:
                file.write("run_label,benchmark,75%(us),95%(us),99%(us),max(us),operations,duration(ms),throughput\n")
                file.write("16-07-2026_08-23-37,get,1,2,3,4,5,6,7\n")
            with patch("simulator.inventory_kubernetes.os.getcwd", return_value=directory):
                configmap = _grafana_dashboard_configmap(plan())
            self.assertTrue(any(key.startswith("sim-report-") for key in configmap["data"]))

    def test_kubectl_logging_redacts_coordinator_parameters(self):
        command = _redacted_command([
            "kubectl", "exec", "pod", "--", "coordinator",
            "--param", "file:client-hazelcast.xml=<license-key>secret</license-key>",
            "--param", "duration=60s",
        ])
        self.assertNotIn("secret", " ".join(command))
        self.assertIn("duration=<redacted>", command)

    @patch("simulator.inventory_kubernetes._hazelcast_pods", return_value=[])
    @patch("simulator.inventory_kubernetes._simulator_role_pods", return_value=[])
    @patch("simulator.inventory_kubernetes._service_json")
    def test_generate_inventory_includes_dc_agents_and_external_endpoints(self, service_json, _agent_pods, _pods):
        service_json.side_effect = lambda _plan, name: load_balancer_service({
            "workers": "203.0.113.10",
            "management-center": "203.0.113.11",
        }[name])
        inventory = generate_inventory(plan())

        self.assertEqual(["workers-0", "workers-1"], list(inventory["dc-a"]["hosts"]))
        self.assertEqual(["workers-2", "workers-3"], list(inventory["dc-b"]["hosts"]))
        hz = inventory["hazelcast"]["hosts"]["workers.simulator.svc"]
        self.assertEqual("workers.simulator.svc", hz["private_ip"])
        self.assertIn("test-k8s-agents-0.test-k8s-agents.simulator.svc", inventory["simulator_agents"]["hosts"])

    @patch("simulator.inventory_kubernetes._hazelcast_pods", return_value=[])
    @patch("simulator.inventory_kubernetes._simulator_role_pods", return_value=[])
    @patch("simulator.inventory_kubernetes._service_json", return_value=None)
    def test_generate_inventory_uses_cluster_dns_without_external_endpoints(self, _service_json, _agent_pods, _pods):
        inventory = generate_inventory(plan())
        self.assertIn("workers.simulator.svc", inventory["hazelcast"]["hosts"])
        self.assertIn("management-center.simulator.svc", inventory["mc"]["hosts"])

    def test_render_manifests_are_owned_and_include_topology_and_grafana_provider(self):
        manifests = render_manifests(plan(observability={"enabled": True}))
        hazelcast = next(doc for doc in manifests if doc["kind"] == "Hazelcast")
        provider = next(doc for doc in manifests if doc.get("metadata", {}).get("name") == "grafana-providers")

        self.assertNotIn("Namespace", [doc["kind"] for doc in manifests])
        self.assertTrue(all(doc["metadata"]["labels"][OWNER_LABEL] == "true" for doc in manifests))
        self.assertTrue(all(doc["metadata"]["labels"][INSTANCE_LABEL] == "test-k8s" for doc in manifests))
        self.assertEqual("ZONE", hazelcast["spec"]["highAvailabilityMode"])
        self.assertIn("providers", provider["data"]["dashboard.yaml"])

    def test_render_manifests_include_in_cluster_simulator_agents(self):
        manifests = render_manifests(plan())
        statefulset = next(doc for doc in manifests if doc["kind"] == "StatefulSet")
        service = next(
            doc for doc in manifests
            if doc["kind"] == "Service" and doc["metadata"]["name"] == "test-k8s-agents"
        )

        self.assertEqual(2, statefulset["spec"]["replicas"])
        self.assertEqual(
            "registry.example.test/hazelcast-simulator:test",
            statefulset["spec"]["template"]["spec"]["containers"][0]["image"],
        )
        affinity = statefulset["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
        expression = affinity["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"][0][
            "matchExpressions"
        ][0]
        self.assertEqual(["zone-a", "zone-b"], expression["values"])
        self.assertEqual("None", service["spec"]["clusterIP"])

    def test_default_manifest_does_not_create_an_empty_custom_config(self):
        manifests = render_manifests(plan())
        hazelcast = next(doc for doc in manifests if doc["kind"] == "Hazelcast")
        self.assertNotIn("customConfigCmName", hazelcast["spec"])
        self.assertNotIn("hazelcast-custom-config", [doc["metadata"]["name"] for doc in manifests])

    def test_synthetic_logical_regions_accept_unequal_counts_and_map_ordinals(self):
        inventory_plan = plan(
            hazelcast={"name": "workers", "cluster_name": "workers", "cluster_size": 5},
            dcs=[
                {"name": "region-a", "members": 2, "pod_ordinals": [0, 1]},
                {"name": "region-b", "members": 2, "pod_ordinals": [2, 3]},
                {"name": "region-c", "members": 1, "pod_ordinals": [4]},
            ],
        )

        validate_inventory_plan(inventory_plan, require_license=False)
        self.assertEqual("region-a", _pod_dc(inventory_plan, {}, "workers-0"))
        self.assertEqual("region-b", _pod_dc(inventory_plan, {}, "workers-3"))
        self.assertEqual("region-c", _pod_dc(inventory_plan, {}, "workers-4"))

    def test_synthetic_regions_do_not_add_physical_zone_affinity(self):
        inventory_plan = plan(
            hazelcast={
                "name": "workers",
                "cluster_name": "workers",
                "cluster_size": 5,
                "scheduling": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": []}},
            },
            dcs=[
                {"name": "region-a", "members": 2, "pod_ordinals": [0, 1]},
                {"name": "region-b", "members": 2, "pod_ordinals": [2, 3]},
                {"name": "region-c", "members": 1, "pod_ordinals": [4]},
            ],
        )

        manifest = next(doc for doc in render_manifests(inventory_plan) if doc["kind"] == "Hazelcast")
        self.assertNotIn("highAvailabilityMode", manifest["spec"])
        self.assertIn("podAntiAffinity", manifest["spec"]["scheduling"])

    @patch("simulator.inventory_kubernetes._hazelcast_pods")
    def test_physical_topology_accepts_the_requested_distribution_in_any_named_zone(self, pods):
        inventory_plan = plan(
            hazelcast={"name": "workers", "cluster_name": "workers", "cluster_size": 7},
            dcs=[
                {"name": "dc-a", "members": 3, "topology_value": "zone-a"},
                {"name": "dc-b", "members": 2, "topology_value": "zone-b"},
                {"name": "dc-c", "members": 2, "topology_value": "zone-c"},
            ],
        )
        pods.return_value = ([{"dc": "dc-a"}] * 2 + [{"dc": "dc-b"}] * 3 + [{"dc": "dc-c"}] * 2)
        _verify_dc_distribution(inventory_plan)

    def test_cp_enabled_requires_persistence(self):
        inventory_plan = plan(hazelcast={"name": "workers", "cluster_size": 5, "cp_enabled": True})
        with self.assertRaises(SystemExit):
            validate_inventory_plan(inventory_plan, require_license=False)

    def test_custom_config_file_is_injected_as_operator_yaml(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as config:
            config.write("hazelcast:\n  cp-subsystem:\n    cp-member-count: 5\n")
            config_path = config.name
        try:
            manifests = render_manifests(plan(
                hazelcast={
                    "name": "workers",
                    "cluster_size": 5,
                    "cp_enabled": True,
                    "persistence": {"enabled": True},
                    "custom_config": {"file": config_path},
                }
            ))
        finally:
            os.remove(config_path)

        manifest = next(
            doc for doc in manifests
            if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == "hazelcast-custom-config"
        )
        self.assertIn("cp-member-count: 5", manifest["data"]["hazelcast.yaml"])

    def test_validation_rejects_external_ssh_loadgenerators(self):
        inventory_plan = plan(
            loadgenerators={"hosts": {"legacy.example": {"ansible_user": "simulator"}}}
        )
        with self.assertRaises(SystemExit):
            validate_inventory_plan(inventory_plan, require_license=False)

    @patch("simulator.inventory_kubernetes._wait_for_resource_condition")
    @patch("simulator.inventory_kubernetes.run_kubectl")
    def test_coordinator_run_uses_lock_and_always_cleans_pod(self, run_kubectl, _wait):
        run_kubectl.return_value = Mock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                with open("inventory.yaml", "w") as inventory:
                    inventory.write("all: {}\n")
                with open("test.properties", "w") as test_file:
                    test_file.write("class=example.Test\n")

                exit_code = run_kubernetes_coordinator(
                    plan(), {"run_path": "runs/test/one"}, "test.properties", "runs/test/one", "abc123"
                )
            finally:
                os.chdir(previous)

        self.assertEqual(0, exit_code)
        commands = [item.args[1] for item in run_kubectl.call_args_list]
        self.assertTrue(any(command[:2] == ["create", "configmap"] for command in commands))
        self.assertTrue(any(command[:2] == ["delete", "pod"] for command in commands))
        self.assertTrue(any(command[:2] == ["delete", "configmap"] for command in commands))

    def test_render_manifests_uses_existing_license_secret_name(self):
        manifests = render_manifests(plan(hazelcast={
            "name": "workers",
            "cluster_name": "workers",
            "cluster_size": 4,
            "existing_license_secret": "shared-license",
        }))
        hazelcast = next(doc for doc in manifests if doc["kind"] == "Hazelcast")
        mc = next(doc for doc in manifests if doc["kind"] == "ManagementCenter")
        self.assertEqual("shared-license", hazelcast["spec"]["licenseKeySecretName"])
        self.assertEqual("shared-license", mc["spec"]["licenseKeySecretName"])
        self.assertNotIn("Secret", [doc["kind"] for doc in manifests])

    def test_existing_secret_precedence_ignores_unused_missing_license_file(self):
        inventory_plan = plan(hazelcast={
            "name": "workers",
            "cluster_size": 4,
            "existing_license_secret": "shared-license",
            "license_file": "/does/not/exist",
        })
        validate_inventory_plan(inventory_plan)

    def test_whitespace_environment_license_falls_back_to_license_file(self):
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as license_file:
            license_file.write("file-license")
            license_path = license_file.name
        try:
            inventory_plan = plan(hazelcast={
                "name": "workers",
                "cluster_size": 4,
                "license_file": license_path,
            })
            with patch.dict(os.environ, {"HZ_LICENSEKEY": "   "}):
                validate_inventory_plan(inventory_plan)
                manifests = render_manifests(inventory_plan)
        finally:
            os.remove(license_path)

        secret = next(doc for doc in manifests if doc["kind"] == "Secret")
        self.assertEqual("file-license", secret["stringData"]["license-key"])

    @patch("simulator.inventory_kubernetes.run_kubectl")
    def test_existing_license_secret_requires_license_key_data(self, run_kubectl):
        run_kubectl.return_value = Mock(returncode=0, stdout=json.dumps({"data": {"other": "value"}}))
        inventory_plan = plan(hazelcast={
            "name": "workers",
            "cluster_size": 4,
            "existing_license_secret": "shared-license",
        })
        with self.assertRaises(SystemExit):
            _verify_license_secret(inventory_plan)

    def test_static_provider_is_normalized_to_existing(self):
        self.assertEqual("existing", kubernetes_provider(plan()))

    @patch("simulator.inventory_kubernetes.run_kubectl")
    @patch("simulator.inventory_kubernetes._service_json")
    def test_nodeport_endpoint_uses_external_node_address_and_allocated_port(self, service_json, run_kubectl):
        service_json.return_value = {
            "spec": {
                "type": "NodePort",
                "ports": [{"port": 5701, "targetPort": 5701, "nodePort": 31234}],
            },
        }
        run_kubectl.return_value = Mock(
            returncode=0,
            stdout=json.dumps({
                "items": [{"status": {"addresses": [{"type": "ExternalIP", "address": "203.0.113.20"}]}}],
            }),
        )

        endpoint = _service_endpoint(plan(), "workers", 5701)

        self.assertEqual("203.0.113.20", endpoint["host"])
        self.assertEqual(31234, endpoint["host_data"]["port"])

    @patch("simulator.inventory_kubernetes._service_endpoint")
    def test_management_center_endpoint_includes_resolved_nodeport(self, service_endpoint):
        service_endpoint.return_value = {
            "host": "203.0.113.20",
            "host_data": {"port": 32080},
        }
        self.assertEqual("http://203.0.113.20:32080", management_center_endpoint(plan()))

    @patch("simulator.inventory_kubernetes._hazelcast_pods", return_value=[])
    def test_control_kill_members_dry_run_uses_direct_pod_selector(self, _pods):
        result = control_kill_members(plan(chaosmesh={"enabled": True}), "dc-b", 30, True)

        manifest = result["manifests"][0]
        self.assertEqual("pod-failure", manifest["spec"]["action"])
        self.assertEqual(["workers-2"], manifest["spec"]["selector"]["pods"]["simulator"])
        self.assertEqual(2, len(result["manifests"]))

    @patch("simulator.inventory_kubernetes._wait_for_named_pod")
    @patch("simulator.inventory_kubernetes.run_kubectl")
    @patch("simulator.inventory_kubernetes._hazelcast_pods", return_value=[])
    def test_control_kill_members_fallback_waits_for_replacement(self, _pods, run_kubectl, wait_for_pod):
        run_kubectl.return_value = Mock(returncode=0)

        result = control_kill_members(plan(), "workers-0", 0, False)

        self.assertEqual("delete-pods", result["action"])
        run_kubectl.assert_called_once_with(plan(), ["delete", "pod", "workers-0", "-n", "simulator"])
        wait_for_pod.assert_called_once_with(plan(), "workers-0")

    @patch("simulator.inventory_kubernetes._hazelcast_pods")
    def test_split_brain_accepts_inventory_pod_names(self, pods):
        pods.return_value = [
            {"name": "workers-0", "dc": "dc-a"},
            {"name": "workers-1", "dc": "dc-b"},
        ]
        result = control_split_brain(
            plan(chaosmesh={"enabled": True}),
            "workers-0/workers-1",
            60,
            True,
        )
        selector = result["manifest"]["spec"]["selector"]["pods"]["simulator"]
        target = result["manifest"]["spec"]["target"]["selector"]["pods"]["simulator"]
        self.assertEqual(["workers-0"], selector)
        self.assertEqual(["workers-1"], target)

    def test_validation_rejects_incomplete_dc_topology(self):
        invalid = plan(dcs=[{"name": "dc-a", "members": 2}, {"name": "dc-b", "members": 2}])
        with self.assertRaises(SystemExit):
            validate_inventory_plan(invalid, require_license=False)

    @patch("simulator.inventory_kubernetes._resource_json")
    def test_apply_refuses_resource_with_only_matching_instance_label(self, resource_json):
        resource_json.return_value = {
            "metadata": {"labels": {INSTANCE_LABEL: "test-k8s"}},
        }
        manifests = [{
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "existing", "namespace": "simulator"},
        }]

        with self.assertRaises(SystemExit):
            _verify_resource_ownership(plan(), manifests)

    @patch("simulator.inventory_kubernetes._write_provider_state")
    @patch("simulator.inventory_kubernetes._gke_get_credentials")
    @patch("simulator.inventory_kubernetes._read_provider_state")
    @patch("simulator.inventory_kubernetes._run_cmd")
    def test_gke_apply_preserves_created_ownership_when_cluster_exists(
            self, run_cmd, read_state, _credentials, write_state):
        run_cmd.return_value = Mock(returncode=0)
        read_state.return_value = {
            "provider": "gke",
            "project": "project",
            "cluster": "simulator",
            "location_type": "region",
            "location": "europe-west1",
            "created": True,
        }
        inventory_plan = plan(
            kubernetes={"provider": "gke", "instance": "test-k8s", "namespace": "simulator"},
            gke={"project_id": "project", "cluster_name": "simulator", "region": "europe-west1"},
        )

        _gke_apply(inventory_plan, force=False)

        write_state.assert_called_once_with({
            "provider": "gke",
            "project": "project",
            "cluster": "simulator",
            "location_type": "region",
            "location": "europe-west1",
            "created": True,
        })

    @patch("simulator.inventory_kubernetes._read_provider_state")
    @patch("simulator.inventory_kubernetes._run_cmd")
    def test_gke_destroy_does_not_inherit_ownership_from_another_project(self, run_cmd, read_state):
        read_state.return_value = {
            "provider": "gke",
            "project": "old-project",
            "cluster": "simulator",
            "location_type": "region",
            "location": "europe-west1",
            "created": True,
        }
        inventory_plan = plan(
            kubernetes={"provider": "gke", "instance": "test-k8s", "namespace": "simulator"},
            gke={"project_id": "new-project", "cluster_name": "simulator", "region": "europe-west1"},
        )

        _gke_destroy(inventory_plan, force=False)

        run_cmd.assert_not_called()

    @patch("simulator.inventory_kubernetes.time.time")
    @patch("simulator.inventory_kubernetes.run_kubectl")
    def test_wait_retries_when_resource_disappears_between_get_and_wait(self, run_kubectl, current_time):
        current_time.side_effect = [0, 0, 1, 1, 2]
        run_kubectl.side_effect = [
            Mock(returncode=0),
            Mock(returncode=1, stderr="NotFound"),
            Mock(returncode=0),
            Mock(returncode=0),
        ]

        _wait_for_resource_condition(plan(), "pod", "workers-0", "ready", "simulator")

        self.assertEqual(4, run_kubectl.call_count)

    @patch("simulator.inventory_kubernetes._resource_is_owned")
    @patch("simulator.inventory_kubernetes.run_kubectl")
    def test_destroy_never_deletes_namespace_without_explicit_opt_in(self, run_kubectl, resource_is_owned):
        resource_is_owned.return_value = True
        docs = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "simulator"}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "owned", "namespace": "simulator"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                os.mkdir(GENERATED_DIR)
                with open(GENERATED_MANIFEST, "w") as f:
                    yaml.safe_dump_all(docs, f)
                _delete_rendered_manifests(plan())
            finally:
                os.chdir(previous)

        self.assertIn(
            call(plan(), ["delete", "configmap", "owned", "--ignore-not-found=true", "-n", "simulator"]),
            run_kubectl.call_args_list,
        )
        for kubectl_call in run_kubectl.call_args_list:
            self.assertNotEqual(["delete", "namespace"], kubectl_call.args[1][:2])

    @patch("simulator.inventory_kubernetes._delete_rendered_manifests")
    @patch("simulator.inventory_kubernetes._cluster_accessible", return_value=False)
    def test_static_destroy_preserves_state_when_cluster_is_unreachable(self, _accessible, delete_manifests):
        with self.assertRaises(SystemExit):
            kubernetes_destroy(plan())
        delete_manifests.assert_not_called()


if __name__ == "__main__":
    unittest.main()
