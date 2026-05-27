import json
import os
import tempfile
import unittest
from unittest.mock import patch

from simulator.inventory_terraform import terraform_import


class TestInventoryTerraform(unittest.TestCase):

    def test_import_supports_legacy_nested_output_shape(self):
        output = {
            "nodes": {
                "value": [[
                    {
                        "public_ip": "203.0.113.10",
                        "private_ip": "10.0.0.10",
                        "tags": {
                            "passthrough:ansible_user": "ubuntu"
                        }
                    }
                ]]
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.check_output", return_value=json.dumps(output)):
                current = os.getcwd()
                try:
                    os.chdir(tmpdir)
                    terraform_import("aws")
                    with open("inventory.yaml") as inventory_file:
                        inventory = inventory_file.read()
                finally:
                    os.chdir(current)

        self.assertIn("203.0.113.10", inventory)
        self.assertIn("private_ip: 10.0.0.10", inventory)
        self.assertIn("public_ip: 203.0.113.10", inventory)
        self.assertIn("ansible_user: ubuntu", inventory)

    def test_import_supports_flat_output_shape(self):
        output = {
            "nodes": {
                "value": [
                    {
                        "public_ip": "203.0.113.20",
                        "private_ip": "10.0.1.20",
                        "tags": {
                            "passthrough:dc": "dc-a"
                        }
                    }
                ]
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.check_output", return_value=json.dumps(output)):
                current = os.getcwd()
                try:
                    os.chdir(tmpdir)
                    terraform_import("aws")
                    with open("inventory.yaml") as inventory_file:
                        inventory = inventory_file.read()
                finally:
                    os.chdir(current)

        self.assertIn("203.0.113.20", inventory)
        self.assertIn("private_ip: 10.0.1.20", inventory)
        self.assertIn("dc: dc-a", inventory)


if __name__ == "__main__":
    unittest.main()
