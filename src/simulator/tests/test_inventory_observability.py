import unittest
from unittest.mock import patch

import inventory_cli


class TestInventoryObservability(unittest.TestCase):

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
