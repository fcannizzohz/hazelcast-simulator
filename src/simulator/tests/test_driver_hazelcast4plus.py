import importlib.util
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def load_driver_configure():
    filename = os.path.join(
        os.environ["SIMULATOR_HOME"],
        "java", "drivers", "driver-hazelcast4plus", "conf", "configure.py",
    )
    spec = importlib.util.spec_from_file_location("driver_hazelcast4plus_configure", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDriverHazelcast4Plus(unittest.TestCase):

    def test_client_config_uses_inventory_port(self):
        module = load_driver_configure()
        args = SimpleNamespace(
            test={"client_hazelcast_xml": "client.xml", "member_port": 5701},
            coordinator_params={},
        )
        nodes = [{"private_ip": "203.0.113.20", "port": 31234}]

        with patch.object(module.os.path, "exists", return_value=True), \
                patch.object(module, "read_file", return_value="<!--MEMBERS-->"):
            module._configure_client_hazelcast_xml(nodes, args)

        self.assertEqual(
            "<address>203.0.113.20:31234</address>",
            args.coordinator_params["file:client-hazelcast.xml"],
        )

    def test_exec_resolves_members_independently_from_worker_hosts(self):
        module = load_driver_configure()
        args = SimpleNamespace(
            test={"member_hosts": "hazelcast", "node_hosts": "loadgenerators", "node_count": 0},
            inventory_path="inventory.yaml",
            coordinator_params={},
        )

        with patch.object(module, "load_hosts", return_value=[]) as load_hosts, \
                patch.object(module, "_configure_log4j_xml"), \
                patch.object(module, "_configure_worker_sh"), \
                patch.object(module, "_configure_hazelcast_xml"), \
                patch.object(module, "_configure_client_hazelcast_xml"):
            module.exec(args)

        load_hosts.assert_called_once_with(inventory_path="inventory.yaml", host_pattern="hazelcast")
        self.assertEqual(0, args.coordinator_params["NODE_WORKER_COUNT"])


if __name__ == "__main__":
    unittest.main()
