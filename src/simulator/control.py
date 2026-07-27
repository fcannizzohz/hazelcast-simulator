#!/usr/bin/env python3
import argparse
import json
import sys
import time
import yaml
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

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


def is_kubernetes_inventory_plan(inventory_plan):
    return inventory_plan.get("provisioner") == "kubernetes"


def load_control_inventory_plan():
    from simulator.util import load_yaml_file

    return load_yaml_file("inventory_plan.yaml")


def resolve_hosts(host_pattern):
    from inventory import load_hosts

    hosts = load_hosts(host_pattern=host_pattern)
    if not hosts:
        fail(f"Could not resolve any hosts for [{host_pattern}]")
    return hosts


def resolve_single_host(host_pattern, role_name):
    hosts = resolve_hosts(host_pattern)
    if len(hosts) != 1:
        fail(f"Expected exactly one {role_name} host for [{host_pattern}], found [{len(hosts)}]")
    return hosts[0]


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


def build_diagnostics_url(mc_host, cluster_name, mc_port):
    encoded_cluster = quote(cluster_name, safe="")
    address = public_ip(mc_host)
    if address.startswith("http://") or address.startswith("https://"):
        base = address.rstrip("/")
    else:
        base = f"http://{address}:{mc_port}"
    return f"{base}/rest/clusters/{encoded_cluster}/diagnostics/config"


def build_diagnostics_payload(enabled, auto_off_minutes):
    return {
        "enabled": enabled,
        "autoOffDurationInMinutes": auto_off_minutes,
    }


def call_diagnostics_api(mc_host, cluster_name, mc_port, method, payload=None):
    url = build_diagnostics_url(mc_host, cluster_name, mc_port)
    body = None
    headers = {}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return {
                "url": url,
                "status": response.status,
                "body": json.loads(response_body) if response_body else None,
            }
    except HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        fail(
            f"Management Center diagnostics API returned HTTP {e.code} for [{method} {url}]. "
            f"The API requires Enterprise MC licensing and a configured cluster connection. "
            f"Response: {response_body}"
        )
    except URLError as e:
        fail(f"Could not reach Management Center diagnostics API at [{url}]: {e.reason}")


def require_dynamic_diagnostics(status_response):
    body = status_response.get("body") or {}
    metadata = body.get("diagnosticsConfigMetadata") or {}
    can_configure = metadata.get("canBeConfiguredDynamically")
    if can_configure is False:
        fail("Diagnostics cannot be configured dynamically for this cluster/member set.")


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
            diagnostics-off          Disable member diagnostics through Management Center
            diagnostics-on           Enable member diagnostics through Management Center
            diagnostics-status       Read member diagnostics state through Management Center
            graceful-restart-members  Gracefully stop member workers, wait, then restart them
            kill-members              Kill member workers, wait, then restart them
            chaos-list                List built-in/custom chaos profiles and executions
            chaos-render              Render a custom Chaos Mesh profile without applying it
            chaos-run                 Run a custom Chaos Mesh profile
            chaos-status              Read tracked Chaos Mesh execution status
            chaos-stop                Stop a tracked Chaos Mesh execution
            member_restart  Restarts dead managed member workers from their worker directories
            member_signal   Sends TERM or KILL to live managed member workers
            probe           Probes managed node hosts and reports agent/worker state
            split-brain     Partitions selected inventory groups using the active provider backend
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

    def chaos_list(self, argv):
        parser = argparse.ArgumentParser(description="List Chaos Mesh profiles and tracked executions")
        parser.parse_args(argv)
        inventory_plan = self._kubernetes_control_inventory()
        from simulator.chaos_kubernetes import chaos_list
        info(json.dumps(chaos_list(inventory_plan), indent=2, sort_keys=True))

    def chaos_render(self, argv):
        parser = argparse.ArgumentParser(description="Render a configured Chaos Mesh profile")
        parser.add_argument("--profile", required=True)
        parser.add_argument("--duration", help="Override the configured experiment duration.")
        parser.add_argument("--allow-elevated", action="store_true")
        args = parser.parse_args(argv)
        inventory_plan = self._kubernetes_control_inventory()
        from simulator.chaos_kubernetes import chaos_render
        result = chaos_render(inventory_plan, args.profile, args.duration, args.allow_elevated)
        info(yaml.safe_dump(result, sort_keys=False))

    def chaos_run(self, argv):
        parser = argparse.ArgumentParser(description="Run a configured Chaos Mesh profile")
        parser.add_argument("--profile", required=True)
        parser.add_argument("--duration", help="Override the configured experiment duration.")
        parser.add_argument("--detach", action="store_true")
        parser.add_argument("--allow-elevated", action="store_true")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(argv)
        require_yes_if_not_dry_run(args, "run chaos profile")
        inventory_plan = self._kubernetes_control_inventory()
        from simulator.chaos_kubernetes import chaos_run
        result = chaos_run(
            inventory_plan, args.profile, args.duration, args.detach, args.dry_run, args.allow_elevated
        )
        info(json.dumps(result, indent=2, sort_keys=True))

    def chaos_status(self, argv):
        parser = argparse.ArgumentParser(description="Read tracked Chaos Mesh execution status")
        parser.add_argument("--execution-id")
        args = parser.parse_args(argv)
        inventory_plan = self._kubernetes_control_inventory()
        from simulator.chaos_kubernetes import chaos_status
        info(json.dumps(chaos_status(inventory_plan, args.execution_id), indent=2, sort_keys=True))

    def chaos_stop(self, argv):
        parser = argparse.ArgumentParser(description="Stop a tracked Chaos Mesh execution")
        parser.add_argument("--execution-id", required=True)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--yes", action="store_true")
        args = parser.parse_args(argv)
        require_yes_if_not_dry_run(args, "stop chaos execution")
        inventory_plan = self._kubernetes_control_inventory()
        from simulator.chaos_kubernetes import chaos_stop
        info(json.dumps(chaos_stop(inventory_plan, args.execution_id, args.dry_run), indent=2, sort_keys=True))

    def _kubernetes_control_inventory(self):
        inventory_plan = load_control_inventory_plan()
        if not is_kubernetes_inventory_plan(inventory_plan):
            fail("Configurable Chaos Mesh profiles are supported only for Kubernetes inventories")
        return inventory_plan

    def probe(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Probe managed node hosts for simulator agent/worker state')
        parser.add_argument("--hosts", required=True, help="Explicit target hosts.")

        args = parser.parse_args(argv)

        from simulator.util import run_parallel

        inventory_plan = load_control_inventory_plan()
        if is_kubernetes_inventory_plan(inventory_plan):
            from simulator.inventory_kubernetes import control_probe
            log_header("Control probe")
            info(json.dumps(control_probe(inventory_plan, args.hosts), indent=2, sort_keys=True))
            log_header("Control probe: Done")
            return

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

    def diagnostics_status(self, argv):
        args = self._diagnostics_args(argv, "Read member diagnostics state through Management Center")
        inventory_plan = self._load_control_inventory()
        mc_host = self._resolve_management_center(inventory_plan, args.mc_hosts)

        log_header("Control diagnostics status")
        info(f"mc_hosts={args.mc_hosts}")
        info(f"cluster={args.cluster}")
        response = call_diagnostics_api(mc_host, args.cluster, args.mc_port, "GET")
        info(json.dumps({
            "management_center": public_ip(mc_host),
            "diagnostics": response,
        }, indent=2, sort_keys=True))
        log_header("Control diagnostics status: Done")

    def diagnostics_on(self, argv):
        args = self._diagnostics_args(argv, "Enable member diagnostics through Management Center", include_auto_off=True)
        if args.auto_off_minutes < 0:
            fail("--auto-off-minutes must be non-negative")

        inventory_plan = self._load_control_inventory()
        mc_host = self._resolve_management_center(inventory_plan, args.mc_hosts)

        log_header("Control diagnostics on")
        info(f"mc_hosts={args.mc_hosts}")
        info(f"cluster={args.cluster}")
        info(f"auto_off_minutes={args.auto_off_minutes}")
        status = call_diagnostics_api(mc_host, args.cluster, args.mc_port, "GET")
        require_dynamic_diagnostics(status)
        update = call_diagnostics_api(
            mc_host,
            args.cluster,
            args.mc_port,
            "POST",
            build_diagnostics_payload(True, args.auto_off_minutes),
        )
        status = call_diagnostics_api(mc_host, args.cluster, args.mc_port, "GET")
        info(json.dumps({
            "management_center": public_ip(mc_host),
            "update": update,
            "diagnostics": status,
        }, indent=2, sort_keys=True))
        log_header("Control diagnostics on: Done")

    def diagnostics_off(self, argv):
        args = self._diagnostics_args(argv, "Disable member diagnostics through Management Center")

        inventory_plan = self._load_control_inventory()
        mc_host = self._resolve_management_center(inventory_plan, args.mc_hosts)

        log_header("Control diagnostics off")
        info(f"mc_hosts={args.mc_hosts}")
        info(f"cluster={args.cluster}")
        status = call_diagnostics_api(mc_host, args.cluster, args.mc_port, "GET")
        require_dynamic_diagnostics(status)
        update = call_diagnostics_api(
            mc_host,
            args.cluster,
            args.mc_port,
            "POST",
            build_diagnostics_payload(False, 0),
        )
        status = call_diagnostics_api(mc_host, args.cluster, args.mc_port, "GET")
        info(json.dumps({
            "management_center": public_ip(mc_host),
            "update": update,
            "diagnostics": status,
        }, indent=2, sort_keys=True))
        log_header("Control diagnostics off: Done")

    def _diagnostics_args(self, argv, description, include_auto_off=False):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description=description)
        parser.add_argument("--mc-hosts", default="mc", help="Management Center inventory host pattern.")
        parser.add_argument("--mc-port", type=int, default=8080, help="Management Center HTTP port.")
        parser.add_argument("--cluster", default="workers", help="Management Center cluster name.")
        if include_auto_off:
            parser.add_argument("--auto-off-minutes", type=int, default=60,
                                help="Minutes after which MC automatically disables diagnostics. Use 0 for no timeout.")
        return parser.parse_args(argv)

    def _load_control_inventory(self):
        inventory_plan = load_control_inventory_plan()
        if not is_kubernetes_inventory_plan(inventory_plan):
            ensure_managed_aws_inventory_plan(inventory_plan)
        return inventory_plan

    def _resolve_management_center(self, inventory_plan, mc_hosts):
        if is_kubernetes_inventory_plan(inventory_plan):
            from simulator.inventory_kubernetes import management_center_endpoint
            return {"public_ip": management_center_endpoint(inventory_plan)}
        return resolve_single_host(mc_hosts, "Management Center")

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

        from simulator.util import run_parallel

        inventory_plan = load_control_inventory_plan()
        if is_kubernetes_inventory_plan(inventory_plan):
            if signal_name == "term":
                from simulator.inventory_kubernetes import control_graceful_restart_members
                result = control_graceful_restart_members(
                    inventory_plan, args.hosts, args.lapse_seconds, args.dry_run, args.start_spread_seconds
                )
            else:
                from simulator.inventory_kubernetes import control_kill_members
                result = control_kill_members(
                    inventory_plan, args.hosts, args.lapse_seconds, args.dry_run, args.start_spread_seconds
                )
            log_header(header)
            info(json.dumps(result, indent=2, sort_keys=True))
            log_header(f"{header}: Done")
            return

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

    def split_brain(self, argv):
        parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                         description='Temporarily partitions selected inventory groups')
        parser.add_argument("--partitions", required=True,
                            help="Partition grammar: group-a/group-b or host1,host2/host3,host4.")
        parser.add_argument("--lapse-seconds", type=int, required=True,
                            help="Seconds to keep the partition active.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Show the generated provider action without applying it.")
        parser.add_argument("--yes", action="store_true",
                            help="Required unless --dry-run is set.")
        args = parser.parse_args(argv)

        if args.lapse_seconds < 0:
            fail("--lapse-seconds must be non-negative")
        require_yes_if_not_dry_run(args, "run split-brain")

        inventory_plan = load_control_inventory_plan()
        if is_kubernetes_inventory_plan(inventory_plan):
            from simulator.inventory_kubernetes import control_split_brain
            log_header("Control split-brain")
            result = control_split_brain(inventory_plan, args.partitions, args.lapse_seconds, args.dry_run)
            info(json.dumps(result, indent=2, sort_keys=True))
            log_header("Control split-brain: Done")
            return

        ensure_managed_aws_inventory_plan(inventory_plan)
        fail("split-brain is not implemented for AWS inventories yet.")


if __name__ == '__main__':
    InventoryControlCli(sys.argv[1:])
