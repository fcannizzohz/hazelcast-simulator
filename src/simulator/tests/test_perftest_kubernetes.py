import unittest
from unittest.mock import patch

from simulator.inventory_kubernetes import prepare_kubernetes_agents
from simulator.perftest import PerfTest


class TestPerftestKubernetes(unittest.TestCase):

    def test_external_members_use_only_loadgenerators_as_agents(self):
        test = {"node_count": 0, "loadgenerator_hosts": "loadgenerators"}
        self.assertEqual("loadgenerators", PerfTest()._agent_host_pattern(test))

    @patch.object(PerfTest, "_kubernetes_inventory_plan", return_value={"provisioner": "kubernetes"})
    def test_external_members_use_loadgenerators_as_coordinator_node_hosts(self, _plan):
        test = {
            "name": "kubernetes-external-members",
            "duration": "1s",
            "driver": "hazelcast5",
            "node_count": 0,
            "loadgenerator_hosts": "loadgenerators",
            "test": {"class": "example.Test"},
        }

        PerfTest()._sanitize_test(test)

        self.assertEqual("loadgenerators", test["node_hosts"])

    @patch("simulator.perftest.load_yaml_file", return_value={"provisioner": "kubernetes"})
    @patch("simulator.perftest.path.exists", return_value=True)
    def test_clean_uses_simulator_agents_for_kubernetes_inventory(self, _exists, _load):
        self.assertEqual("simulator_agents", PerfTest()._agent_host_pattern())

    @patch("simulator.inventory_kubernetes.path.isdir", return_value=False)
    @patch("simulator.remote.remote_exec")
    def test_kubernetes_dstat_files_are_unique_and_reportable(self, remote_exec, _isdir):
        agents = [
            {"provider": "kubernetes", "pod": "smoke-agents-0", "public_ip": "smoke-agents-0"},
            {"provider": "kubernetes", "pod": "smoke-agents-1", "public_ip": "smoke-agents-1"},
        ]

        prepare_kubernetes_agents({}, agents, "run-1")

        commands = [call.args[1] for call in remote_exec.call_args_list]
        self.assertIn("--output /opt/simulator/workers/run-1/smoke-agents-0_dstat.csv", commands[1])
        self.assertIn("--output /opt/simulator/workers/run-1/smoke-agents-1_dstat.csv", commands[3])


if __name__ == "__main__":
    unittest.main()
