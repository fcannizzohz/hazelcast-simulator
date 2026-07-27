import unittest
from unittest.mock import Mock, patch

from inventory import load_hosts


class TestInventory(unittest.TestCase):

    @patch("inventory.os.path.exists", return_value=True)
    @patch("inventory.subprocess.run")
    def test_load_hosts_preserves_endpoint_port(self, run, _exists):
        run.side_effect = [
            Mock(stdout="  hosts (1):\n    hz.example.net\n"),
            Mock(stdout="""all:
  children:
    hazelcast:
      hosts:
        hz.example.net:
          private_ip: hz.example.net
          port: 31234
    nodes:
      hosts:
        hz.example.net:
          private_ip: hz.example.net
          port: 31234
"""),
        ]

        hosts = load_hosts("inventory.yaml", "hazelcast")

        self.assertEqual(31234, hosts[0]["port"])
        self.assertEqual(1, len(hosts))

    @patch("inventory.os.path.exists", return_value=True)
    @patch("inventory.subprocess.run")
    def test_load_hosts_preserves_kubernetes_transport_metadata(self, run, _exists):
        run.side_effect = [
            Mock(stdout="  hosts (1):\n    agent.simulator.svc\n"),
            Mock(stdout="""all:
  children:
    loadgenerators:
      hosts:
        agent.simulator.svc:
          provider: kubernetes
          pod: simulator-agents-0
          namespace: simulator
          context: test-context
          private_ip: agent.simulator.svc
"""),
        ]

        host = load_hosts("inventory.yaml", "loadgenerators")[0]

        self.assertEqual("kubernetes", host["provider"])
        self.assertEqual("simulator-agents-0", host["pod"])
        self.assertEqual("simulator", host["namespace"])
        self.assertEqual("test-context", host["context"])


if __name__ == "__main__":
    unittest.main()
