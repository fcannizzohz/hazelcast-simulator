# Simulator Observability

This directory contains the Prometheus and Grafana stack installed by Simulator.
Prometheus scrapes an existing Management Center `/metrics` endpoint; Management Center is not
started by this stack.

## Prerequisites

Use an AWS template that has both the `mc` and `observability` inventory groups. The install command
requires exactly one provisioned `mc` host and at least one `observability` host.

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

Provision the infrastructure and install the stack:

```bash
inventory apply
inventory install observability
```

The install command renders Prometheus to scrape `http://<mc-private-ip>:8080/metrics`, uploads this
directory to `~/hazelcast-observability` on the observability host, and starts Docker Compose.

If `mc` is not provisioned, the command exits before running Ansible and tells you to set
`mc.count: 1`.

## Access

After installation, open:

- Grafana: `http://<observability-public-ip>:3000`
- Prometheus: `http://<observability-public-ip>:9090`

Find the public IP in `inventory.yaml` under the `observability` group.

## Operate

Run these commands from the benchmark directory.

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

## Troubleshooting

If Grafana has no data, check that Prometheus can scrape Management Center:

```bash
inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose exec prometheus wget -qO- http://<mc-private-ip>:8080/metrics | head || sudo docker-compose exec prometheus wget -qO- http://<mc-private-ip>:8080/metrics | head)"
```

Replace `<mc-private-ip>` with the `mc` host private IP from `inventory.yaml`.

If `inventory install observability` reports that `mc` is missing, set `mc.count: 1`, run
`inventory apply`, and rerun the install command.

If port `3000` or `9090` is unreachable, verify the `observability` security group was provisioned
and that the stack is running with the status command above.
