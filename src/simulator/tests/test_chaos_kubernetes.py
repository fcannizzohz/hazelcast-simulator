import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from simulator.chaos_kubernetes import (
    BUILTIN_PROFILES,
    builtin_latency,
    chaos_list,
    chaos_render,
    chaos_run,
    validate_chaos_configuration,
)


def plan(profile=None, **chaos_overrides):
    chaos = {
        "enabled": True,
        "namespace": "chaos-mesh",
        "default_duration": "5m",
        "profiles": {"test-profile": profile} if profile else {},
    }
    chaos.update(chaos_overrides)
    return {
        "provisioner": "kubernetes",
        "kubernetes": {"provider": "static", "instance": "test-k8s", "namespace": "simulator"},
        "simulator": {"image": "registry.example.test/simulator:test", "loadgenerators": {"count": 2}},
        "hazelcast": {"name": "workers", "cluster_size": 2},
        "dcs": [{"name": "dc-a", "members": 2}],
        "mc": {"enabled": False},
        "chaosmesh": chaos,
    }


class TestChaosKubernetes(unittest.TestCase):

    def test_builtin_profiles_are_always_listed_without_custom_profiles(self):
        result = chaos_list(plan())
        names = [item["name"] for item in result["profiles"]]
        self.assertEqual(list(BUILTIN_PROFILES), names)

    def test_reserved_builtin_profile_cannot_be_overridden(self):
        inventory_plan = plan()
        inventory_plan["chaosmesh"]["profiles"] = {
            "_builtin.kill-members": {"kind": "PodChaos", "spec": {"action": "pod-kill"}}
        }
        with self.assertRaises(SystemExit):
            validate_chaos_configuration(inventory_plan)

    @patch("simulator.chaos_kubernetes._resolve_targets")
    def test_render_network_delay_injects_inventory_selectors_and_jitter(self, resolve_targets):
        resolve_targets.side_effect = [
            [{"name": "workers-0"}],
            [{"name": "workers-1"}],
        ]
        profile = {
            "kind": "NetworkChaos",
            "targets": "dc-a",
            "target": {"targets": "dc-b", "mode": "all"},
            "mode": "all",
            "duration": "2m",
            "spec": {
                "action": "delay",
                "direction": "both",
                "delay": {"latency": "100ms", "jitter": "20ms", "correlation": "25"},
            },
        }

        result = chaos_render(plan(profile), "test-profile", execution_id="abc123")

        spec = result["manifests"][0]["spec"]
        self.assertEqual(["workers-0"], spec["selector"]["pods"]["simulator"])
        self.assertEqual(["workers-1"], spec["target"]["selector"]["pods"]["simulator"])
        self.assertEqual("20ms", spec["delay"]["jitter"])
        self.assertEqual("2m", spec["duration"])

    @patch("simulator.chaos_kubernetes._resolve_targets", return_value=[{"name": "workers-0"}])
    def test_chaos_run_dry_run_does_not_apply(self, _resolve_targets):
        profile = {
            "kind": "StressChaos",
            "targets": "nodes",
            "duration": "30s",
            "spec": {"mode": "one", "stressors": {"cpu": {"workers": 1, "load": 80}}},
        }
        with patch("simulator.inventory_kubernetes.run_kubectl") as run_kubectl:
            result = chaos_run(plan(profile), "test-profile", dry_run=True)
        self.assertTrue(result["dry_run"])
        run_kubectl.assert_not_called()

    def test_workload_raw_manifest_rejects_external_targets(self):
        document = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "NetworkChaos",
            "metadata": {"name": "unsafe"},
            "spec": {
                "action": "delay",
                "mode": "all",
                "selector": {"namespaces": ["simulator"]},
                "externalTargets": ["203.0.113.10"],
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as manifest:
            yaml.safe_dump(document, manifest)
            filename = manifest.name
        try:
            with self.assertRaises(SystemExit):
                chaos_render(plan({"manifest": filename, "scope": "workload"}), "test-profile")
        finally:
            os.remove(filename)

    def test_elevated_scope_requires_inventory_and_command_opt_in(self):
        inventory_plan = plan({
            "kind": "GCPChaos",
            "scope": "cloud",
            "spec": {"action": "node-stop", "mode": "one"},
        }, allow_elevated_scope=True)

        with self.assertRaises(SystemExit):
            chaos_render(inventory_plan, "test-profile")
        result = chaos_render(inventory_plan, "test-profile", allow_elevated=True)
        self.assertEqual("cloud", result["scope"])

    def test_raw_manifest_expands_inventory_placeholders(self):
        document = {
            "apiVersion": "chaos-mesh.org/v1alpha1",
            "kind": "PodChaos",
            "metadata": {"name": "placeholder"},
            "spec": {
                "action": "pod-failure",
                "mode": "one",
                "selector": {"namespaces": ["${WORKLOAD_NAMESPACE}"]},
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as manifest:
            yaml.safe_dump(document, manifest)
            filename = manifest.name
        try:
            result = chaos_render(plan({"manifest": filename}), "test-profile")
        finally:
            os.remove(filename)
        self.assertEqual(["simulator"], result["manifests"][0]["spec"]["selector"]["namespaces"])

    @patch("simulator.chaos_kubernetes._resolve_targets", return_value=[{"name": "workers-0"}])
    def test_builtin_latency_supports_jitter_and_correlation(self, _resolve_targets):
        manifest = builtin_latency(plan(), "nodes", None, 100, 20, 25, "1m")
        self.assertEqual(
            {"latency": "100ms", "jitter": "20ms", "correlation": "25"},
            manifest["spec"]["delay"],
        )

    @patch("simulator.chaos_kubernetes._resolve_targets", return_value=[{"name": "workers-0"}])
    def test_profiles_file_keeps_chaos_out_of_the_inventory_plan(self, _resolve_targets):
        profile = {
            "file-profile": {
                "kind": "PodChaos",
                "targets": "nodes",
                "duration": "30s",
                "spec": {"action": "pod-failure"},
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as profiles_file:
            yaml.safe_dump(profile, profiles_file)
            filename = profiles_file.name
        try:
            inventory_plan = plan(None, profiles_file=filename)
            validate_chaos_configuration(inventory_plan)
            result = chaos_render(inventory_plan, "file-profile", execution_id="abc123")
        finally:
            os.remove(filename)
        self.assertEqual("PodChaos", result["manifests"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
