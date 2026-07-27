import unittest
from unittest.mock import Mock, patch

from simulator.remote import copy_to_remote, remote_exec


class TestRemote(unittest.TestCase):

    @patch("simulator.remote.subprocess.run")
    def test_kubernetes_exec_uses_pod_namespace_and_context(self, run):
        run.return_value = Mock(returncode=0, stdout="ok\n", stderr="")
        host = {
            "provider": "kubernetes",
            "public_ip": "agent.simulator.svc",
            "pod": "simulator-agents-0",
            "namespace": "simulator",
            "context": "test-context",
        }

        exitcode, output = remote_exec(host, "java -version")

        self.assertEqual(0, exitcode)
        self.assertEqual("ok\n", output)
        self.assertEqual([
            "kubectl", "--context", "test-context", "-n", "simulator",
            "exec", "simulator-agents-0", "--", "sh", "-lc", "java -version",
        ], run.call_args.args[0])

    @patch("simulator.remote.subprocess.run")
    def test_kubernetes_copy_uses_kubectl_cp(self, run):
        run.return_value = Mock(returncode=0, stdout="", stderr="")
        host = {
            "provider": "kubernetes",
            "public_ip": "agent.simulator.svc",
            "pod": "simulator-agents-0",
            "namespace": "simulator",
        }

        copy_to_remote(host, "driver.jar", "/opt/simulator/driver-lib")

        self.assertEqual([
            "kubectl", "-n", "simulator", "cp", "driver.jar",
            "simulator-agents-0:/opt/simulator/driver-lib",
        ], run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
