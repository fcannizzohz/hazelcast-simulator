# Simulator Observability

This directory contains the Prometheus and Grafana stack installed by Simulator.
Prometheus scrapes an existing Management Center `/metrics` endpoint; Management Center is not
started by this stack.

## Prerequisites

Use an AWS template that has both the `mc` and `observability` inventory groups, or a Kubernetes template with
`mc.enabled: true` and `observability.enabled: true`.

For AWS, the install command requires exactly one provisioned `mc` host and at least one `observability` host.

Set both groups in `inventory_plan.yaml`:

```yaml
mc:
    count: 1

observability:
    count: 1
```

The AWS templates start Management Center with the Prometheus exporter enabled:

```text
-Dhazelcast.mc.prometheusExporter.enabled=true
-Dhazelcast.mc.prometheusExporter.timestamp.enabled=false
-Dhazelcast.mc.prometheusExporter.printers=V1
```

## Install

### AWS

Provision the infrastructure and install the stack:

```bash
inventory apply
inventory install observability
```

The install command configures Management Center to connect to the `nodes` group as cluster `workers`,
restarts Management Center, renders Prometheus to scrape `http://<mc-private-ip>:8080/metrics`, uploads
this directory to `~/hazelcast-observability` on the observability host, and starts Docker Compose.

If `HZ_LICENSEKEY` is present in the environment, the Management Center restart includes
`MC_LICENSE=...` and `-Dhazelcast.mc.license=...`. This is optional for Community Edition clusters, but required for Enterprise-only
MC features such as unrestricted Prometheus exporter use. The value is read from the Ansible controller
environment, so it is not printed in the local Ansible command line. The Java system property can be visible in the
MC JVM process arguments on the remote host while MC is running.

Before running `hz-mc conf`, the installer stops the current MC process and removes a stale
`~/hazelcast-mc/mc.lock` file if one remains. This avoids failed reconfiguration after an earlier MC process did
not exit cleanly. The installer sets `MC_HOME=~/hazelcast-mc` for both `hz-mc conf` and the restarted MC process,
so the cluster connection is saved in the same MC home that the process uses at runtime.

Use these options if the default cluster connection does not match your project:

```bash
inventory install observability --member-hosts nodes --member-port 5701 --cluster-name workers
```

If `mc` is not provisioned, the command exits before running Ansible and tells you to set
`mc.count: 1`.

### Kubernetes

Kubernetes observability is installed by the Kubernetes installer rather than the AWS Ansible installer:

```bash
inventory apply
inventory install k8s
```

The installer renders Prometheus and Grafana into `.simulator-k8s/generated.yaml` when
`observability.enabled: true`. Prometheus scrapes the Management Center service in the same namespace. Grafana uses
the same dashboard JSON files and provisioning provider from `observability/grafana` that the AWS stack uses.

For OpenShift-style exposure, set:

```yaml
kubernetes:
    provider: openshift
    service_type: Route
```

## Access

After installation, open:

- Grafana: `http://<observability-public-ip>:3000`
- Prometheus on AWS: `http://<observability-public-ip>:9090`

Kubernetes Prometheus is intentionally a cluster-internal service. Access it with
`kubectl port-forward service/prometheus 9090:9090 -n <namespace>` when direct inspection is needed. The Kubernetes install waits for
Management Center, Prometheus, and Grafana before completing. Cluster service DNS names are written to the `mc` and
`observability` inventory groups; explicit external exposure remains optional.

Kubernetes chaos executions append start, injection, recovery, failure, and stop records to
`.simulator-k8s/chaos-events.jsonl`. The coordinator copies the current event stream into the run directory as
`chaos-events.jsonl`, allowing report and Grafana timelines to be correlated with latency, jitter, partitions, and
other Chaos Mesh profiles. When Grafana is enabled, the same lifecycle transitions are published through its annotation
API using a temporary port forward. Annotation failure is reported but does not abort the experiment.
`inventory control probe --hosts nodes` also reports tracked active executions.

## Dashboards

Grafana provisions every JSON dashboard from `observability/grafana/dashboards`.
The bundled dashboards include:

- `Cluster Overview`: member count, uptime, clients, heap, CPU, GC, file descriptors, and partition migration.
- `AP Maps`: AP IMap inventory, entry counts, memory, operation rate, latency, hit rate, evictions, and expirations.
- `Operations Reliability`: operation queues, invocations, timeouts, failed backups, executor queues, event queues, and TCP write pressure.
- `Simulator Run Context`: run-level view of cluster size, clients, map throughput, failover/recovery signals, heap, and operation queues.
- `Jet Jobs`: optional Jet job lifecycle, tasklets, queue fill, items in/out, watermark delay, event-time staleness, and snapshots.
- CP subsystem dashboards for Raft, CP health, CP data structures, and CP map operations.

The AP dashboards are based on the Management Center `/metrics` endpoint. They intentionally use only metric
families observed in the live MC scrape model (`hz_*` via MC). Cache, near-cache, queue, topic, ringbuffer, and Jet
AP data-structure panels should be added only when those metric families are present in the target workload. The Jet
dashboard remains empty or zero until a Jet workload exposes `hz_jobs_*`, `hz_taskletCount`, `hz_queues*`,
watermark, and snapshot metrics.

## Operate

Run these commands from the benchmark directory.

### Diagnostics

Member workers are started with a diagnostics directory preconfigured under each worker directory, but diagnostics are
disabled until you turn them on. Use Management Center to toggle diagnostics dynamically without restarting workers:

```bash
inventory control diagnostics-status --cluster workers
inventory control diagnostics-on --cluster workers --auto-off-minutes 60
inventory control diagnostics-off --cluster workers
```

The commands call the Management Center diagnostics configuration REST API on the `mc` inventory group. For Kubernetes
inventories, simulator resolves the Management Center service or Route from the active Kubernetes context. The API
requires Enterprise Management Center licensing and a configured cluster connection. Management Center can toggle
diagnostics on and off, but it cannot change the diagnostics log directory dynamically; the default worker script
preconfigures member diagnostics output under `<worker-dir>/diagnostics` so generated files are downloaded with the
normal run artifacts.

Use `--cluster`, `--mc-hosts`, and `--mc-port` if your project does not use the defaults:

```bash
inventory control diagnostics-on --cluster workers --mc-hosts mc --mc-port 8080 --auto-off-minutes 60
```

After the run finishes, diagnostics files, if any were produced, are available under each downloaded worker directory:

```text
runs/<test>/<timestamp>/<worker>/diagnostics/
```

### Stack

Check container status:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose ps || sudo docker-compose ps)"
```

View recent logs:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose logs --tail=100 || sudo docker-compose logs --tail=100)"
```

Restart Prometheus and Grafana:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose restart || sudo docker-compose restart)"
```

Pull the latest Prometheus and Grafana images and restart:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose pull && sudo docker compose up -d || sudo docker-compose pull && sudo docker-compose up -d)"
```

Reinstall after local dashboard or rule changes:

```bash
inventory install observability
```

Destroy the whole AWS environment, including observability:

```bash
inventory destroy
```

## Export a local Grafana bundle

Export a completed run before destroying its observability stack. The command
copies the run artifacts, generates portable report dashboards, snapshots the
complete retained Prometheus TSDB, and writes a self-contained Docker Compose
bundle. The default destination includes the current export timestamp:

```bash
docker-sim perftest export_observability runs/<test>/<run-timestamp>
# runs/<test>/<run-timestamp>/observability-export-YYYYMMDD-HHMMSS/
```

Use `--output-dir` to choose a destination or `--overwrite` to replace one:

```bash
docker-sim perftest export_observability runs/<test>/<run-timestamp> \
  --output-dir /path/to/export --overwrite
```

Change into the exported directory and start its local stack:

```bash
cd runs/<test>/<run-timestamp>/observability-export-YYYYMMDD-HHMMSS
docker compose up -d
```

Browse Grafana at `http://localhost:3000` and Prometheus at
`http://localhost:9090`. The **Hazelcast** folder uses the Prometheus snapshot;
the **Simulator Run** folder contains report dashboards with their run data
embedded through Grafana TestData. Stop the local stack with `docker compose down`.

The snapshot contains every metric retained by the source Prometheus instance,
not only the selected run. Treat the export directory as a potentially sensitive
artifact. The source Prometheus must still be reachable when the export runs;
the local bundle remains usable after `inventory destroy`.

## Troubleshooting

If Grafana has no data, check that Prometheus can scrape Management Center:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose exec prometheus wget -qO- http://<mc-private-ip>:8080/metrics | head || sudo docker-compose exec prometheus wget -qO- http://<mc-private-ip>:8080/metrics | head)"
```

Replace `<mc-private-ip>` with the `mc` host private IP from `inventory.yaml`.

If the scrape succeeds but Hazelcast metrics are absent, verify that Management Center has a configured
cluster connection and that member workers are running:

```bash
inventory install observability --member-hosts nodes --cluster-name workers
inventory shell --hosts mc "tail -100 ~/mc.out"
```

If `inventory install observability` reports that `mc` is missing, set `mc.count: 1`, run
`inventory apply`, and rerun the install command.

If port `3000` or `9090` is unreachable, verify the `observability` security group was provisioned
and that the stack is running with the status command above.
