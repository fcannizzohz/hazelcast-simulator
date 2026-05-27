This template is an additive scaffold for managed multi-DC AWS deployments.

In this first step, it only adds a new project shape and a multi-DC `inventory_plan.yaml`.
It does not yet add provisioning logic, so `inventory apply` is expected to be implemented
in a later change.

Create a project:

```shell
perftest create --template hazelcast5-multidc-ec2 my-multidc-benchmark
```

Inspect the generated `inventory_plan.yaml` and adjust:
- shared instance defaults under `nodes`, `loadgenerators`, and `mc`
- the `dcs:` list
- per-DC counts, regions, AZs, and CIDRs

Backward compatibility notes:
- existing templates are unchanged
- existing single-DC workflows remain unchanged
- `existing-cluster` is untouched
