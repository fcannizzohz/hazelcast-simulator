# Hazelcast Simulator on Kubernetes

This template runs Hazelcast members, Simulator coordinator, and load
generators as Kubernetes Pods. The local CLI only creates resources, streams
logs, and retrieves results. Use the same lifecycle as AWS projects:

```bash
inventory apply
inventory install k8s
perftest run
inventory destroy
```

Set `simulator.image` before installing. Every cluster node must be able to
pull it. The image is used by the coordinator and load-generator Pods.

## Minimal Inventory

`inventory_plan.yaml` is intentionally small. It defaults to an existing
Kubernetes cluster, three members, one load generator, and no optional add-ons.

```yaml
provisioner: kubernetes
kubernetes:
    provider: existing
    namespace: simulator
operator:
    install: true
hazelcast:
    cluster_size: 3
simulator:
    image: registry.example.com/hazelcast-simulator:latest
    loadgenerators: {count: 1}
```

`existing` uses the current kubeconfig context and never creates or deletes the
cluster. Set `kubernetes.context` when needed. The legacy `static` provider is
accepted for existing projects but new plans should use `existing`. For GKE,
set `provider: gke` and add the `gke` block described in
[`examples/k8s/README.md`](../../examples/k8s/README.md).

All Simulator workloads stay in Kubernetes. `loadgenerators.hosts` is not
supported by Kubernetes plans. The generated inventory still uses familiar
groups: `nodes`, `dc-a`, `hazelcast`, `loadgenerators`, and
`simulator_agents`.

## Optional Features

Enable Management Center and observability together:

```yaml
mc: {enabled: true}
observability: {enabled: true}
```

For physical multi-DC placement, use node label values returned by the GKE
helper or supplied by the cluster administrator. The member counts are a
distribution target: a `3-2-2` plan guarantees that distribution across the
three domains but does not pin the three-member group to a named zone. Use
`pod_ordinals` only for exact synthetic logical regions.

```yaml
hazelcast: {cluster_size: 7}
dcs:
    - {name: dc-a, members: 3, topology_value: europe-west1-b}
    - {name: dc-b, members: 2, topology_value: europe-west1-c}
    - {name: dc-c, members: 2, topology_value: europe-west1-d}
```

Enable persistence through the Hazelcast CR. CP subsystem, maps, and other
member behavior belong in a separate full Hazelcast YAML file:

```yaml
hazelcast:
    cp_enabled: true
    persistence: {enabled: true, request_storage: 20Gi}
    custom_config: {file: hazelcast.yaml}
```

`hazelcast.yaml` must contain a top-level `hazelcast:` mapping. Do not put
persistence, networking, or security into this file because the Operator owns
the required Kubernetes resources.

## Chaos Mesh Add-on

Chaos Mesh is off by default. For a dedicated test cluster, enable the add-on
and let Simulator install it:

```yaml
chaosmesh:
    enabled: true
    install: true
    profiles_file: chaos/profiles.yaml
```

For a shared cluster, set `install: false` only after an administrator has
installed Chaos Mesh. `chaos/profiles.yaml` contains named, workload-scoped
experiments. The included `same-dc-latency` adds delay and jitter without a
failure; `rolling-failures` uses the raw Workflow in
`chaos/rolling-failures.yaml`.

```bash
inventory control chaos-render --profile same-dc-latency
inventory control chaos-run --profile same-dc-latency --detach --yes
inventory control chaos-status
inventory control chaos-stop --execution-id <id> --yes
```

All experiments are ownership-labelled and removed by `chaos-stop` or
`inventory destroy`.

## Upgrades and Diagnosis

Upgrade members by changing `hazelcast.version` and rerunning
`inventory install k8s`. The Operator performs the rolling replacement; check
completion with `kubectl get hazelcast -n simulator`. Keep a PDB with
`max_unavailable: 1`, use compatible versions, and never combine an upgrade
with an active partition unless that interaction is the test.

For a rolling restart without changing the image:

```bash
inventory control graceful-restart-members \
  --hosts nodes --lapse-seconds 30 --start-spread-seconds 60 --yes
```

See [`examples/k8s/README.md`](../../examples/k8s/README.md) for beginner
setup, GKE values, the three-node Chaos Mesh smoke test, observability,
multi-DC failure testing, and the synthetic AP/CP scenario.
