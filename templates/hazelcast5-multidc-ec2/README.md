This template is an additive scaffold for managed multi-DC AWS deployments.

The first managed implementation now supports:
- multiple DC entries in one AWS region
- bounded cross-region support for up to two AWS regions
- one subnet per DC
- one shared VPC and Internet Gateway per region
- optional per-DC AMI overrides for nodes, load generators, and MC
- optional Prometheus/Grafana observability host
- public IPs for operator access
- private IPs for Hazelcast, load generator, and MC-to-cluster traffic

When two regions are used, private cross-region traffic is carried over VPC peering.

Create a project:

```shell
perftest create --template hazelcast5-multidc-ec2 my-multidc-benchmark
```

Inspect the generated `inventory_plan.yaml` and adjust:
- shared instance defaults under `nodes`, `loadgenerators`, `mc`, and `observability`
- the `dcs:` list
- per-DC counts, regions, AZs, and CIDRs

Provision and inspect:

```shell
inventory apply
cat inventory.yaml
```

For the first managed cut, all `dcs` entries must:
- use at most two AWS regions
- share one `vpc_id` and one `internet_gateway_id` per region
- use different `availability_zone` and `cidr_block` values

If an AMI does not exist in every target region, keep the current top-level role AMI as the default and override only the DCs that need a different image:

```yaml
dcs:
  - name: dc-a
    ...
    nodes:
      count: 1
      ami: ami-region-a-node
    loadgenerators:
      count: 1
      ami: ami-region-a-loadgen
    mc:
      ami: ami-region-a-mc
    observability:
      ami: ami-region-a-observability

  - name: dc-b
    ...
    nodes:
      count: 2
      ami: ami-region-b-node
```

## Observability

The template can place Prometheus and Grafana in a selected DC. Management Center
must also be provisioned because Prometheus scrapes the MC `/metrics` endpoint.

Set `mc.count: 1`, `observability.count: 1`, and choose placement with `mc_dc`
and `observability_dc`:

```yaml
mc_dc: dc-a
observability_dc: dc-a

mc:
  count: 1

observability:
  count: 1
```

Install the stack after provisioning:

```shell
inventory apply
inventory install observability
```

The installer configures Management Center to connect to the `nodes` group as
cluster `workers`, restarts MC, then starts Prometheus and Grafana. If
`HZ_LICENSEKEY` is present, the restart also applies it with
`MC_LICENSE` and `-Dhazelcast.mc.license`; the value is not passed on the local
Ansible command line, but the Java system property can be visible in the MC JVM
process arguments on the remote host while MC is running. The installer sets
`MC_HOME=~/hazelcast-mc` for both `hz-mc conf` and MC startup so the configured
cluster connection is used at runtime. Use `--member-hosts`, `--member-port`, or
`--cluster-name` if the project uses a different cluster layout.

Open Grafana on `http://<observability-public-ip>:3000` and Prometheus on
`http://<observability-public-ip>:9090`.

The install command prints the Management Center, Grafana, and Prometheus
endpoints at the end of a successful run.

## Control commands

The `inventory control` commands operate on the flat simulator inventory output.
Use explicit worker host groups, normally `nodes`, so MC and observability hosts
are not targeted:

```shell
inventory control probe --hosts nodes
inventory control graceful-restart-members --hosts nodes --dry-run
inventory control graceful-restart-members --hosts nodes --lapse-seconds 30 --yes
```

When Management Center is provisioned and connected to the cluster, member
diagnostics can be toggled dynamically without restarting workers:

```shell
inventory control diagnostics-status --cluster workers
inventory control diagnostics-on --cluster workers --auto-off-minutes 60
inventory control diagnostics-off --cluster workers
```

The default member worker script preconfigures diagnostics output under each
worker directory, so generated diagnostics files are downloaded with the normal
run artifacts.

Backward compatibility notes:
- existing templates are unchanged
- existing single-DC workflows remain unchanged
- `existing-cluster` is untouched

Test examples and a manual smoke-test runbook are available in
[examples/multi-dc/README.md](../../examples/multi-dc/README.md).
