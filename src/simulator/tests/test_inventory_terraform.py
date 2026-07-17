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

    def test_import_supports_map_output_shape(self):
        output = {
            "nodes": {
                "value": {
                    "dc-b-node-0": {
                        "public_ip": "203.0.113.30",
                        "private_ip": "10.0.2.30",
                        "tags": {
                            "passthrough:dc": "dc-b",
                            "passthrough:region": "eu-west-2"
                        }
                    }
                }
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

        self.assertIn("203.0.113.30", inventory)
        self.assertIn("private_ip: 10.0.2.30", inventory)
        self.assertIn("dc: dc-b", inventory)
        self.assertIn("region: eu-west-2", inventory)

    def test_import_supports_observability_output_group(self):
        output = {
            "observability": {
                "value": [
                    {
                        "public_ip": "203.0.113.40",
                        "private_ip": "10.0.3.40",
                        "tags": {
                            "passthrough:ansible_user": "ubuntu",
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

        self.assertIn("observability:", inventory)
        self.assertIn("203.0.113.40", inventory)
        self.assertIn("private_ip: 10.0.3.40", inventory)
        self.assertIn("public_ip: 203.0.113.40", inventory)
        self.assertIn("ansible_user: ubuntu", inventory)
        self.assertIn("dc: dc-a", inventory)


if __name__ == "__main__":
    unittest.main()
