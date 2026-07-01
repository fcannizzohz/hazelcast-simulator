import unittest
import json
import shlex
from unittest.mock import patch

import inventory_cli


class TestInventoryObservability(unittest.TestCase):

    def test_ansible_extra_vars_quotes_shell_command_as_json(self):
        command = 'ps -ef | grep -E "[h]azelcast-management-center|[h]z-mc"; tail -80 ~/mc.out'
        quoted = inventory_cli.ansible_extra_vars(cmd=command)
        parsed = shlex.split(quoted)
        self.assertEqual(1, len(parsed))
        self.assertEqual(command, json.loads(parsed[0])["cmd"])

    def test_management_center_metrics_target_prefers_private_ip(self):
        target = inventory_cli.management_center_metrics_target({
            "private_ip": "10.0.20.10",
            "public_ip": "54.0.0.10",
        })
        self.assertEqual("10.0.20.10:8080", target)

    def test_management_center_metrics_target_falls_back_to_public_ip(self):
        target = inventory_cli.management_center_metrics_target({
            "public_ip": "54.0.0.10",
        })
        self.assertEqual("54.0.0.10:8080", target)

    def test_management_center_metrics_target_requires_address(self):
        with self.assertRaises(SystemExit):
            inventory_cli.management_center_metrics_target({})

    def test_public_endpoint_prefers_public_ip(self):
        endpoint = inventory_cli.public_endpoint({
            "private_ip": "10.0.20.10",
            "public_ip": "54.0.0.10",
        }, 8080, "Management Center")
        self.assertEqual("http://54.0.0.10:8080", endpoint)

    def test_public_endpoint_falls_back_to_private_ip(self):
        endpoint = inventory_cli.public_endpoint({
            "private_ip": "10.0.20.10",
        }, 3000, "observability")
        self.assertEqual("http://10.0.20.10:3000", endpoint)

    def test_public_endpoint_requires_address(self):
        with self.assertRaises(SystemExit):
            inventory_cli.public_endpoint({}, 9090, "observability")

    def test_management_center_member_addresses_prefers_private_ips(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[
            {"private_ip": "10.0.10.10", "public_ip": "54.0.0.10"},
            {"private_ip": "10.0.10.11", "public_ip": "54.0.0.11"},
        ]):
            addresses = inventory_cli.management_center_member_addresses("nodes", 5701)
        self.assertEqual("10.0.10.10:5701,10.0.10.11:5701", addresses)

    def test_management_center_member_addresses_falls_back_to_public_ips(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[
            {"public_ip": "54.0.0.10"},
            {"public_ip": "54.0.0.11"},
        ]):
            addresses = inventory_cli.management_center_member_addresses("nodes", 5702)
        self.assertEqual("54.0.0.10:5702,54.0.0.11:5702", addresses)

    def test_management_center_member_addresses_requires_address(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[{}]):
            with self.assertRaises(SystemExit):
                inventory_cli.management_center_member_addresses("nodes", 5701)

    def test_require_single_inventory_host_rejects_missing_mc(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[]):
            with self.assertRaises(SystemExit):
                inventory_cli.require_single_inventory_host("mc", "Management Center")

    def test_require_single_inventory_host_rejects_multiple_mc_hosts(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[{}, {}]):
            with self.assertRaises(SystemExit):
                inventory_cli.require_single_inventory_host("mc", "Management Center")

    def test_require_inventory_hosts_rejects_empty_observability_group(self):
        with patch.object(inventory_cli, "load_hosts", return_value=[]):
            with self.assertRaises(SystemExit):
                inventory_cli.require_inventory_hosts("observability", "observability")


if __name__ == "__main__":
    unittest.main()
