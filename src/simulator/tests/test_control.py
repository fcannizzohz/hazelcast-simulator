import unittest

from simulator.control import (
    InventoryControlCli,
    build_host_schedule,
    ensure_managed_aws_inventory_plan,
    normalize_command_name,
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


if __name__ == "__main__":
    unittest.main()
