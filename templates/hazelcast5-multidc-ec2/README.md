This template is an additive scaffold for managed multi-DC AWS deployments.

The first managed implementation now supports:
- multiple DC entries in one AWS region
- bounded cross-region support for up to two AWS regions
- one subnet per DC
- one shared VPC and Internet Gateway per region
- optional per-DC AMI overrides for nodes, load generators, and MC
- public IPs for operator access
- private IPs for Hazelcast, load generator, and MC-to-cluster traffic

When two regions are used, private cross-region traffic is carried over VPC peering.

Create a project:

```shell
perftest create --template hazelcast5-multidc-ec2 my-multidc-benchmark
```

Inspect the generated `inventory_plan.yaml` and adjust:
- shared instance defaults under `nodes`, `loadgenerators`, and `mc`
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

  - name: dc-b
    ...
    nodes:
      count: 2
      ami: ami-region-b-node
```

Backward compatibility notes:
- existing templates are unchanged
- existing single-DC workflows remain unchanged
- `existing-cluster` is untouched

Test examples and a manual smoke-test runbook are available in
[examples/multi-dc/README_TEST.md](/Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/README_TEST.md).
