import unittest
from unittest.mock import patch

from simulator.perftest import PerfTest


class TestPerftestKubernetes(unittest.TestCase):

    def test_external_members_use_only_loadgenerators_as_agents(self):
        test = {"node_count": 0, "loadgenerator_hosts": "loadgenerators"}
        self.assertEqual("loadgenerators", PerfTest()._agent_host_pattern(test))

    @patch("simulator.perftest.load_yaml_file", return_value={"provisioner": "kubernetes"})
    @patch("simulator.perftest.path.exists", return_value=True)
    def test_clean_uses_simulator_agents_for_kubernetes_inventory(self, _exists, _load):
        self.assertEqual("simulator_agents", PerfTest()._agent_host_pattern())


if __name__ == "__main__":
    unittest.main()
