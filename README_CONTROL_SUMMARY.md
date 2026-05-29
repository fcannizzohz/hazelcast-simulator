# Control Command Summary

This change adds a new `inventory control ...` command family for failure testing on
Terraform-managed AWS simulator projects.

## What Problem It Solves

Simulator could run benchmarks on managed AWS clusters, but it did not have a focused,
host-level way to inject member failures and then bring the same member back.

This change adds that missing control path so we can:

- inspect live simulator agent and worker state on a remote host
- kill only member workers instead of all Java on the machine
- restart the same logical member from its existing worker directory
- run controlled member failure cycles with a configurable lapse and staggered start

## Scope

The new control flow is intentionally narrow:

- supported only for `provisioner: terraform` with `terraform_plan: aws`
- supported only for explicit `--hosts` selections
- does not touch `existing-cluster`
- operates on member workers, not broad host-level Java process cleanup

## New Commands

The following commands are now available through `inventory control`:

- `probe`
  - inspects agent state and discovered workers on one or more hosts
- `member_signal`
  - experiment helper to send `TERM` or `KILL` to live member workers only
- `member_restart`
  - experiment helper to restart dead member workers from their worker directory
- `graceful-restart-members`
  - sends `SIGTERM`, waits a configured lapse, then restarts the member
- `kill-members`
  - sends `SIGKILL`, waits a configured lapse, then restarts the member

Failure-cycle commands support:

- `--hosts`
- `--lapse-seconds`
- `--start-spread-seconds`
- `--dry-run`
- `--yes`

## What Was Proven

The implementation was validated on managed AWS projects from a laptop using the normal
simulator remote-control flow.

The key behaviors that were proven:

- `probe` can discover the remote simulator home, agent PID, run ID, worker directories,
  worker type, and worker PID state
- `member_signal` can target only the live `member` worker without using blanket
  `kill_java`
- `member_restart` can restart the same logical member from the existing worker
  directory without restarting the agent
- `kill-members` and `graceful-restart-members` can perform a full stop/wait/restart
  cycle against explicit hosts

On a multi-member run, the restarted member came back under the same run ID with a new
PID, which is the behavior needed for failure/rejoin testing.

## Supporting Fixes Included

This branch also includes a few supporting fixes that were discovered while testing the
new control flow:

- lazy import of report code in `perftest_cli.py`
  - avoids pulling in report-only dependencies for unrelated commands
- lazy import of `boto3` in `inventory_terraform.py`
  - avoids requiring the AWS SDK just to load unrelated inventory commands
- `PerfTest.run()` now attempts report collection even when a run exits nonzero
  - useful for intentional failure tests that still produce reportable artifacts
- `PerftestCollectCli` now passes warmup and cooldown as integers instead of one-item
  lists

## Current Caveat

When using the published `hazelcast/simulator:latest` image, report generation works
best when the project directory is mounted to `/workspace` without overriding the image's
own `/opt/simulator` contents.

Mounting a local checkout over `/opt/simulator` can change which wrapper scripts and
Python interpreter are used inside the container, which can affect dependency behavior.
