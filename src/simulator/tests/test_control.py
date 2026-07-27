import unittest
from unittest.mock import patch

from simulator.control import (
    InventoryControlCli,
    build_diagnostics_payload,
    build_diagnostics_url,
    build_host_schedule,
    call_diagnostics_api,
    ensure_managed_aws_inventory_plan,
    normalize_command_name,
    require_dynamic_diagnostics,
)


class TestControl(unittest.TestCase):

    def test_accepts_managed_aws_inventory(self):
        ensure_managed_aws_inventory_plan({
            "provisioner": "terraform",
            "terraform_plan": "aws",
        })

    def test_rejects_non_terraform_inventory(self):
        with self.assertRaises(SystemExit):
            ensure_managed_aws_inventory_plan({
                "provisioner": "static",
            })

    def test_rejects_non_aws_terraform_plan(self):
        with self.assertRaises(SystemExit):
            ensure_managed_aws_inventory_plan({
                "provisioner": "terraform",
                "terraform_plan": "gcp",
            })

    def test_member_signal_requires_yes_without_dry_run(self):
        with self.assertRaises(SystemExit):
            InventoryControlCli(["member_signal", "--hosts", "1.2.3.4", "--signal", "term"])

    def test_member_restart_requires_yes_without_dry_run(self):
        with self.assertRaises(SystemExit):
            InventoryControlCli(["member_restart", "--hosts", "1.2.3.4"])

    def test_kill_members_requires_yes_without_dry_run(self):
        with self.assertRaises(SystemExit):
            InventoryControlCli(["kill-members", "--hosts", "1.2.3.4", "--lapse-seconds", "1"])

    def test_normalize_command_name(self):
        self.assertEqual("kill_members", normalize_command_name("kill-members"))
        self.assertEqual("member_signal", normalize_command_name("member_signal"))

    def test_build_host_schedule_spreads_offsets(self):
        hosts = [
            {"public_ip": "10.0.0.3"},
            {"public_ip": "10.0.0.1"},
            {"public_ip": "10.0.0.2"},
        ]
        schedule = build_host_schedule(hosts, 120)
        self.assertEqual(
            [
                ("10.0.0.1", 0),
                ("10.0.0.2", 60),
                ("10.0.0.3", 120),
            ],
            [(host["public_ip"], offset) for host, offset in schedule],
        )

    def test_build_diagnostics_url_encodes_cluster_name(self):
        url = build_diagnostics_url({"public_ip": "10.0.0.1"}, "workers dc-a", 8080)
        self.assertEqual(
            "http://10.0.0.1:8080/rest/clusters/workers%20dc-a/diagnostics/config",
            url,
        )

    def test_build_diagnostics_url_accepts_absolute_management_center_url(self):
        url = build_diagnostics_url({"public_ip": "https://mc.apps.example.test"}, "workers", 8080)
        self.assertEqual(
            "https://mc.apps.example.test/rest/clusters/workers/diagnostics/config",
            url,
        )

    def test_build_diagnostics_payload(self):
        self.assertEqual(
            {
                "enabled": True,
                "autoOffDurationInMinutes": 15,
            },
            build_diagnostics_payload(True, 15),
        )

    def test_require_dynamic_diagnostics_accepts_missing_metadata(self):
        require_dynamic_diagnostics({"body": {}})

    def test_require_dynamic_diagnostics_rejects_static_config(self):
        with self.assertRaises(SystemExit):
            require_dynamic_diagnostics({
                "body": {
                    "diagnosticsConfigMetadata": {
                        "canBeConfiguredDynamically": False,
                    },
                },
            })

    def test_call_diagnostics_api_parses_json_response(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                pass

            def read(self):
                return b'{"enabled": true}'

        with patch("simulator.control.urlopen", return_value=Response()) as urlopen:
            response = call_diagnostics_api({"public_ip": "10.0.0.1"}, "workers", 8080, "GET")

        self.assertEqual(200, response["status"])
        self.assertEqual({"enabled": True}, response["body"])
        self.assertEqual("GET", urlopen.call_args.args[0].get_method())

    def test_call_diagnostics_api_posts_json_payload(self):
        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                pass

            def read(self):
                return b""

        payload = build_diagnostics_payload(False, 0)
        with patch("simulator.control.urlopen", return_value=Response()) as urlopen:
            response = call_diagnostics_api({"public_ip": "10.0.0.1"}, "workers", 8080, "POST", payload)

        request = urlopen.call_args.args[0]
        self.assertEqual(204, response["status"])
        self.assertIsNone(response["body"])
        self.assertEqual("POST", request.get_method())
        self.assertEqual(b'{"enabled": false, "autoOffDurationInMinutes": 0}', request.data)


if __name__ == "__main__":
    unittest.main()
