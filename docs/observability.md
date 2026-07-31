# Observability and Portable Reports

## Why

Observability combines Management Center, Prometheus, and Grafana so a run can
be inspected while it is active. The export feature preserves dashboards, run
artifacts, and retained metrics locally, allowing results to remain browseable
after temporary infrastructure is removed.

## Live stack

AWS plans can provision a dedicated `observability` host; Kubernetes plans can
deploy the stack into the target namespace. In both cases Prometheus scrapes the
Management Center metrics endpoint, so Management Center must be configured and
available. For an EC2 project, provision the `mc` and `observability` groups,
then run:

```bash
docker-sim inventory install observability
```

For a Kubernetes project, set `mc.enabled: true` and
`observability.enabled: true` and use `docker-sim inventory install k8s`; the
Kubernetes installer deploys the stack, waits for Management Center, Prometheus,
and Grafana, and verifies that Prometheus sees the Management Center metrics
target as healthy. Both installers print the available endpoints on completion.
A license supplied through `HZ_LICENSEKEY` is applied
to Management Center, but JVM process arguments can make that value visible
while Management Center is running.

## Exporting a completed run

Before destroying the source stack, ensure `observability.enabled: true` is in
the project plan and export the completed run:

```bash
docker-sim perftest export_observability runs/<test>/<run-timestamp>
```

By default the bundle is written below the run as
`observability-export-YYYYMMDD-HHMMSS`. `--output-dir` selects another location
and `--overwrite` replaces an existing destination. The command copies run
artifacts, generates report dashboards backed by Grafana TestData, snapshots
the retained Prometheus TSDB, and writes a self-contained Docker Compose stack.

Start the exported bundle with `docker compose up -d`; Grafana is available on
port 3000 and Prometheus on port 9090. The Prometheus snapshot includes all
metrics retained by the source, not only the selected run, so treat the bundle
as a potentially sensitive artifact.

The [Kubernetes observability tutorial](../examples/k8s/README.md#12-use-observability)
shows live access and the full inventory lifecycle.

The offline bundle discovers all reportable run directories below the project's
`runs/` directory and provisions one Simulator dashboard per run in Grafana.
Incomplete runs without `report/report.csv` are skipped and listed in the export
output.
