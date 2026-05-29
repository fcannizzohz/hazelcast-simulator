#!/usr/bin/env python3
import argparse
import json
import sys
import time

from simulator.hosts import public_ip, ssh_options, ssh_user
from simulator.log import error, info, log_header


def fail(text):
    error(text)
    raise SystemExit(1)


def ensure_managed_aws_inventory_plan(inventory_plan):
    provisioner = inventory_plan.get("provisioner")
    if provisioner != "terraform":
        fail(f"Inventory control is only supported for terraform inventories, found [{provisioner}]")

    terraform_plan = inventory_plan.get("terraform_plan")
    if terraform_plan != "aws":
        fail(f"Inventory control is only supported for terraform plan [aws], found [{terraform_plan}]")


def resolve_hosts(host_pattern):
    from inventory import load_hosts

    hosts = load_hosts(host_pattern=host_pattern)
    if not hosts:
        fail(f"Could not resolve any hosts for [{host_pattern}]")
    return hosts


def run_probe(host):
    from simulator.ssh import Ssh

    ssh = Ssh(public_ip(host), ssh_user(host), ssh_options(host))
    _, output = ssh.exec("hazelcast-simulator/bin/hidden/control_probe")
    return public_ip(host), json.loads(output)


def run_member_signal(host, signal_name, dry_run):
    from simulator.ssh import Ssh

    ssh = Ssh(public_ip(host), ssh_user(host), ssh_options(host))
    dry_run_arg = "true" if dry_run else "false"
    _, output = ssh.exec(
        f"hazelcast-simulator/bin/hidden/control_member_signal {signal_name} {dry_run_arg}"
    )
    return public_ip(host), json.loads(output)


def run_member_restart(host, dry_run):
    from simulator.ssh import Ssh

    ssh = Ssh(public_ip(host), ssh_user(host), ssh_options(host))
    dry_run_arg = "true" if dry_run else "false"
    _, output = ssh.exec(
        f"hazelcast-simulator/bin/hidden/control_member_restart {dry_run_arg}"
    )
    return public_ip(host), json.loads(output)


def normalize_command_name(command):
    return command.replace("-", "_")


def build_host_schedule(hosts, start_spread_seconds):
    sorted_hosts = sorted(hosts, key=public_ip)
    if len(sorted_hosts) <= 1:
        return [(sorted_hosts[0], 0)] if sorted_hosts else []

    return [
        (host, int(index * start_spread_seconds / (len(sorted_hosts) - 1)))
        for index, host in enumerate(sorted_hosts)
    ]


def execute_member_cycle(host, signal_name, lapse_seconds, start_offset_seconds, dry_run):
    time.sleep(start_offset_seconds)

    signal_host, signal_result = run_member_signal(host, signal_name, dry_run)

    if not dry_run:
        time.sleep(lapse_seconds)

    restart_host, restart_result = run_member_restart(host, dry_run)
    inventory_host = signal_host if signal_host == restart_host else public_ip(host)

    return inventory_host, {
        "signal": signal_name,
        "dry_run": dry_run,
        "start_offset_seconds": start_offset_seconds,
        "lapse_seconds": lapse_seconds,
        "signal_result": signal_result,
        "restart_result": restart_result,
    }


def require_yes_if_not_dry_run(args, action_name):
    if not args.dry_run and not args.yes:
        fail(f"Refusing to {action_name} without --yes. Use --dry-run to inspect first.")


class InventoryControlCli:

    def __init__(self, argv):
        usage = '''control <command> [<args>]

        The available commands are:
            graceful-restart-members  Gracefully stop member workers, wait, then restart them
            kill-members              Kill member workers, wait, then restart them
            member_restart  Restarts dead managed member workers from their worker directories
            member_signal   Sends TERM or KILL to live managed member workers
            probe           Probes managed node hosts and reports agent/worker state
        '''

        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Probes and controls managed inventory hosts', usage=usage)
        parser.add_argument('command', help='Subcommand to run')

        args = parser.parse_args(argv[:1])
        command = normalize_command_name(args.command)
        if not hasattr(self, command):
            print('Unrecognized command', parser.print_help())
            exit(1)

        getattr(self, command)(argv[1:])

    def probe(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Probe managed AWS node hosts for simulator agent/worker state')
        parser.add_argument("--hosts", required=True, help="Explicit target hosts.")

        args = parser.parse_args(argv)

        from simulator.util import load_yaml_file, run_parallel

        inventory_plan = load_yaml_file("inventory_plan.yaml")
        ensure_managed_aws_inventory_plan(inventory_plan)

        hosts = resolve_hosts(args.hosts)

        log_header("Control probe")
        info(f"hosts={args.hosts}")
        results = run_parallel(run_probe, [(host,) for host in hosts])
        for inventory_host, host_state in sorted(results):
            info(json.dumps({
                "inventory_host": inventory_host,
                "probe": host_state,
            }, indent=2, sort_keys=True))
        log_header("Control probe: Done")

    def member_signal(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Signal live managed member workers on explicit hosts')
        parser.add_argument("--hosts", required=True, help="Explicit target hosts.")
        parser.add_argument("--signal", required=True, choices=["term", "kill"],
                            help="Signal mode: term sends SIGTERM, kill sends SIGKILL.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the targeted member workers without sending a signal.")
        parser.add_argument("--yes", action="store_true",
                            help="Required unless --dry-run is set.")

        args = parser.parse_args(argv)

        require_yes_if_not_dry_run(args, "signal member workers")

        from simulator.util import load_yaml_file, run_parallel

        inventory_plan = load_yaml_file("inventory_plan.yaml")
        ensure_managed_aws_inventory_plan(inventory_plan)

        hosts = resolve_hosts(args.hosts)

        log_header("Control member signal")
        info(f"hosts={args.hosts}")
        info(f"signal={args.signal}")
        info(f"dry_run={args.dry_run}")
        results = run_parallel(run_member_signal, [(host, args.signal, args.dry_run) for host in hosts])
        for inventory_host, host_state in sorted(results):
            info(json.dumps({
                "inventory_host": inventory_host,
                "member_signal": host_state,
            }, indent=2, sort_keys=True))
        log_header("Control member signal: Done")

    def member_restart(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Restart dead managed member workers from their worker directories')
        parser.add_argument("--hosts", required=True, help="Explicit target hosts.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the dead member workers that would be restarted.")
        parser.add_argument("--yes", action="store_true",
                            help="Required unless --dry-run is set.")

        args = parser.parse_args(argv)

        require_yes_if_not_dry_run(args, "restart member workers")

        from simulator.util import load_yaml_file, run_parallel

        inventory_plan = load_yaml_file("inventory_plan.yaml")
        ensure_managed_aws_inventory_plan(inventory_plan)

        hosts = resolve_hosts(args.hosts)

        log_header("Control member restart")
        info(f"hosts={args.hosts}")
        info(f"dry_run={args.dry_run}")
        results = run_parallel(run_member_restart, [(host, args.dry_run) for host in hosts])
        for inventory_host, host_state in sorted(results):
            info(json.dumps({
                "inventory_host": inventory_host,
                "member_restart": host_state,
            }, indent=2, sort_keys=True))
        log_header("Control member restart: Done")

    def graceful_restart_members(self, argv):
        self._member_cycle(argv, "term", "Control graceful restart members")

    def kill_members(self, argv):
        self._member_cycle(argv, "kill", "Control kill members")

    def _member_cycle(self, argv, signal_name, header):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Cycle managed member workers through signal, lapse, and restart')
        parser.add_argument("--hosts", required=True, help="Explicit target hosts.")
        parser.add_argument("--lapse-seconds", type=int, required=True,
                            help="Seconds to wait between stop/kill and restart.")
        parser.add_argument("--start-spread-seconds", type=int, default=0,
                            help="Spread the start of selected host operations across this window.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the planned cycle without sending signals or restarting workers.")
        parser.add_argument("--yes", action="store_true",
                            help="Required unless --dry-run is set.")

        args = parser.parse_args(argv)

        if args.lapse_seconds < 0:
            fail("--lapse-seconds must be non-negative")
        if args.start_spread_seconds < 0:
            fail("--start-spread-seconds must be non-negative")

        require_yes_if_not_dry_run(args, f"run the {signal_name} member cycle")

        from simulator.util import load_yaml_file, run_parallel

        inventory_plan = load_yaml_file("inventory_plan.yaml")
        ensure_managed_aws_inventory_plan(inventory_plan)

        hosts = resolve_hosts(args.hosts)
        schedule = build_host_schedule(hosts, args.start_spread_seconds)

        log_header(header)
        info(f"hosts={args.hosts}")
        info(f"signal={signal_name}")
        info(f"lapse_seconds={args.lapse_seconds}")
        info(f"start_spread_seconds={args.start_spread_seconds}")
        info(f"dry_run={args.dry_run}")
        for host, offset in schedule:
            info(json.dumps({
                "inventory_host": public_ip(host),
                "start_offset_seconds": offset,
            }, sort_keys=True))

        results = run_parallel(
            execute_member_cycle,
            [(host, signal_name, args.lapse_seconds, offset, args.dry_run) for host, offset in schedule],
        )
        for inventory_host, host_state in sorted(results):
            info(json.dumps({
                "inventory_host": inventory_host,
                "member_cycle": host_state,
            }, indent=2, sort_keys=True))
        log_header(f"{header}: Done")


if __name__ == '__main__':
    InventoryControlCli(sys.argv[1:])
