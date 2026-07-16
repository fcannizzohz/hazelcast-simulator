# Control Command Summary

This file should be treated as the working spec for the `inventory control` feature.

This change adds a new `inventory control ...` command family for failure testing and
runtime diagnostics control on Terraform-managed AWS simulator projects.

## What Problem It Solves

Simulator could run benchmarks on managed AWS clusters, but it did not have a focused,
host-level way to inject member failures and then bring the same member back.

This change adds that missing control path so we can:

- inspect live simulator agent and worker state on a remote host
- kill only member workers instead of all Java on the machine
- restart the same logical member from its existing worker directory
- run controlled member failure cycles with a configurable lapse and staggered start
- enable or disable Hazelcast member diagnostics dynamically through Management Center
  without restarting workers

## Scope

The new control flow is intentionally narrow:

- supported only for `provisioner: terraform` with `terraform_plan: aws`
- failure-cycle commands are supported only for explicit `--hosts` selections
- diagnostics commands target the configured Management Center host, defaulting to the
  `mc` inventory group
- does not touch `existing-cluster`
- member failure commands operate on member workers, not broad host-level Java process
  cleanup
- diagnostics commands use the Management Center REST API and require Enterprise MC
  licensing plus a configured cluster connection

## Command Spec

### Implemented Commands

The following commands are implemented today through `inventory control`:

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
- `diagnostics-status`
  - reads Hazelcast diagnostics state through Management Center
- `diagnostics-on`
  - enables Hazelcast diagnostics dynamically through Management Center
  - supports an auto-off timeout
- `diagnostics-off`
  - disables Hazelcast diagnostics dynamically through Management Center

Failure-cycle commands support:

- `--hosts`
- `--lapse-seconds`
- `--start-spread-seconds`
- `--dry-run`
- `--yes`

Diagnostics commands support:

- `--cluster`
  - defaults to `workers`
- `--mc-hosts`
  - defaults to `mc`
  - must resolve to exactly one Management Center host
- `--mc-port`
  - defaults to `8080`
- `--auto-off-minutes`
  - `diagnostics-on` only
  - defaults to `60`
  - use `0` for no timeout

Example diagnostics workflow:

```bash
inventory control diagnostics-status --cluster workers
inventory control diagnostics-on --cluster workers --auto-off-minutes 60
inventory control diagnostics-off --cluster workers
```

The diagnostics commands call:

```text
GET  /rest/clusters/{cluster}/diagnostics/config
POST /rest/clusters/{cluster}/diagnostics/config
```

The POST body is limited by the MC API to:

```json
{
  "enabled": true,
  "autoOffDurationInMinutes": 60
}
```

Management Center can toggle diagnostics dynamically, but it cannot dynamically change
the diagnostics log directory. The member JVM must be preconfigured at startup with the
directory and rolling settings.

### Planned Commands

The following commands are part of the intended `inventory control` design and should
be treated as next steps for the feature.

- `restart-instance`
  - managed AWS only
  - explicit `--hosts` only
  - performs a stop/start instance cycle, not terminate/recreate
  - per host behavior:
    - wait scheduled start offset
    - stop instance
    - wait `--lapse-seconds`
    - start instance
  - should support:
    - `--hosts`
    - `--lapse-seconds`
    - `--start-spread-seconds`
    - `--dry-run`
    - `--yes`

- `split-brain`
  - managed AWS only
  - host-based, not DC-based
  - explicit `--partitions` only
  - creates a temporary network partition across two or more host groups
  - partition grammar:
    - `host1,host2/host3,host4/host5`
    - `,` separates hosts inside one partition
    - `/` separates partitions
    - minimum 2 partitions
    - a host may appear in only one partition
  - network behavior:
    - connectivity inside the same partition remains allowed
    - connectivity between different partitions is blocked
    - all partitions are mutually isolated from each other during the lapse window
  - easiest intended implementation:
    - resolve all hosts to private IPs
    - install temporary host firewall rules to drop traffic from each partition to all
      other partitions
    - wait `--lapse-seconds`
    - remove those rules
  - should support:
    - `--partitions`
    - `--lapse-seconds`
    - `--start-spread-seconds`
    - `--dry-run`
    - `--yes`

### Common Control Rules

All current and planned destructive control commands should follow these rules:

- managed AWS only
- explicit host selection only
- no `existing-cluster` support
- no `--all-members`
- `--yes` required unless `--dry-run`
- host execution order should be deterministic
- `--start-spread-seconds` should spread host start times evenly across the requested
  window

Diagnostics commands are not destructive host-cycle commands, so they do not require
`--hosts`, `--dry-run`, or `--yes`. They should still fail clearly when:

- the inventory is not a managed AWS Terraform project
- the `mc` host selection is missing or resolves to more than one host
- Management Center is unreachable
- Management Center returns an Enterprise-license or cluster-connection error
- MC reports that diagnostics cannot be configured dynamically

## Diagnostics Log Collection

The Hazelcast 4+ worker script now preconfigures member diagnostics before the member
starts:

```text
-Dhazelcast.diagnostics.enabled=false
-Dhazelcast.diagnostics.directory=<worker-dir>/diagnostics
-Dhazelcast.diagnostics.filename.prefix=<worker-name>
-Dhazelcast.diagnostics.max.rolled.file.size.mb=50
-Dhazelcast.diagnostics.max.rolled.file.count=10
```

The default state is disabled so diagnostics do not run unless explicitly enabled
through MC. The directory and rolling properties are still present from startup, which
lets MC turn diagnostics on later without restarting the worker.

If a project already supplies one of these `-Dhazelcast.diagnostics.*` properties in
`member_args`, that explicit value is preserved and the default is not appended for
that property.

Generated diagnostics files are written under each worker directory:

```text
<worker-dir>/diagnostics/
```

The existing Simulator artifact download already rsyncs worker directories at the end
of the run, excluding only `upload`, so diagnostics files are collected automatically
when they exist.

## Grafana Report Dashboards

The `perftest report_grafana` command generates Grafana dashboards from an existing
Simulator run or HTML report directory. It is intended for the user experience where a
run has already completed, `perftest report` has produced a `report/` directory, and
the user wants Grafana dashboards that emulate the report charts without rerunning the
benchmark.

The command takes either the run timestamp directory or the nested report directory as
input:

```bash
perftest report_grafana runs/<run-name>/<timestamp>
perftest report_grafana runs/<run-name>/<timestamp>/report
```

By default the command:

- reads `report.csv`, `data.csv`, `latency/*.csv`, and `operations/*.csv`
- creates a Grafana folder named after the report timestamp
- creates or reuses a TestData datasource named `Simulator Report TestData`
- imports generated dashboards through the Grafana HTTP API
- prints the generated dashboard URLs

The generated dashboards are grouped by the report timestamp and currently include:

- a summary dashboard derived from `report.csv`
- latency dashboards derived from report latency CSV files
- operation throughput dashboards derived from `operations/*.csv`
- system resource dashboards derived from dstat columns in `data.csv`
- an errors dashboard derived from `worker.log` files and `failures.txt` when worker
  errors are present

Each chart includes a description explaining what the chart shows and how to interpret
it. The embedded TestData CSV uses the long form `time,metric,value`, so Grafana sees a
stable numeric `value` field for time-series panels.

Incomplete runs are supported. If `report.csv`, latency CSV files, or operation CSV
files are missing, the command skips those dashboards and still imports dashboards for
the data it can find. For example, a failed startup run that only contains
`report/data.csv` and worker directories will still produce summary, system, and errors
dashboards. The errors dashboard extracts matching `WARN`, `ERROR`, `FATAL`, `SEVERE`,
`Exception`, and `Error` lines from available `worker.log` files.

If Grafana cannot be inferred from `inventory.yaml`, provide it explicitly:

```bash
perftest report_grafana runs/<run-name>/<timestamp>/report --grafana-url http://<grafana-host>:3000
```

To generate JSON files without installing them in Grafana:

```bash
perftest report_grafana runs/<run-name>/<timestamp>/report --no-install --output-dir /tmp/report-dashboards
```

To update dashboards that were already imported, rerun with `--overwrite`:

```bash
perftest report_grafana runs/<run-name>/<timestamp>/report --overwrite
```

No Grafana restart is required when the command imports dashboards through the HTTP API.
If the command is run through a Docker image that contains an older simulator checkout,
rebuild or refresh that image before rerunning the command.

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
- diagnostics control commands build the correct MC REST URLs and POST bodies
- member startup preconfigures diagnostics output under the worker directory without
  overriding explicit `member_args` diagnostics properties

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
- the Hazelcast 4+ worker script preconfigures diagnostics output under the worker
  directory so MC can enable diagnostics dynamically and the normal artifact download
  collects the logs

## Current Caveat

When using the published `hazelcast/simulator:latest` image, report generation works
best when the project directory is mounted to `/workspace` without overriding the image's
own `/opt/simulator` contents.

Mounting a local checkout over `/opt/simulator` can change which wrapper scripts and
Python interpreter are used inside the container, which can affect dependency behavior.
