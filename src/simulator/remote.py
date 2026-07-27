import subprocess

from simulator.log import info
from simulator.ssh import Ssh
from simulator.util import exit_with_error
from simulator.hosts import public_ip, ssh_options, ssh_user


def is_kubernetes_host(host):
    return host.get("provider") == "kubernetes" or bool(host.get("pod"))


def remote_exec(host, command, check=True):
    if not is_kubernetes_host(host):
        return Ssh(public_ip(host), ssh_user(host), ssh_options(host)).exec(command)

    cmd = _kubectl(host) + ["exec", host.get("pod", public_ip(host)), "--", "sh", "-lc", command]
    result = _run(cmd, check)
    return result.returncode, result.stdout or ""


def copy_to_remote(host, source, destination, check=True):
    if not is_kubernetes_host(host):
        return Ssh(public_ip(host), ssh_user(host), ssh_options(host)).scp_to_remote(source, destination)

    pod = host.get("pod", public_ip(host))
    result = _run(_kubectl(host) + ["cp", source, f"{pod}:{destination}"], check)
    return result.returncode


def copy_from_remote(host, source, destination, check=True):
    if not is_kubernetes_host(host):
        from simulator.util import shell
        return shell(
            f'''rsync --copy-links -avvz --compress-level=9 -e "ssh {ssh_options(host)}" '''
            f'''--exclude 'upload' {ssh_user(host)}@{public_ip(host)}:{source} {destination}''')

    pod = host.get("pod", public_ip(host))
    result = _run(_kubectl(host) + ["cp", f"{pod}:{source}", destination], check)
    return result.returncode


def _kubectl(host):
    cmd = ["kubectl"]
    if host.get("context"):
        cmd.extend(["--context", host["context"]])
    cmd.extend(["-n", host.get("namespace", "default")])
    return cmd


def _run(cmd, check):
    info(" ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        exit_with_error(
            f"Remote Kubernetes command failed, exitcode={result.returncode}, "
            f"command=[{' '.join(cmd)}], stderr=[{(result.stderr or '').strip()}]"
        )
    return result
