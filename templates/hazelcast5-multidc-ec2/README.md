This template is an additive scaffold for managed multi-DC AWS deployments.

The first managed implementation now supports:
- multiple DC entries in one AWS region
- one subnet per DC
- same VPC / same Internet Gateway across all DCs
- public IPs for operator access
- private IPs for Hazelcast, load generator, and MC-to-cluster traffic

It does not yet support cross-region provisioning.

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
- use the same AWS region
- use the same `vpc_id`
- use the same `internet_gateway_id`
- use different `availability_zone` and `cidr_block` values

Backward compatibility notes:
- existing templates are unchanged
- existing single-DC workflows remain unchanged
- `existing-cluster` is untouched
