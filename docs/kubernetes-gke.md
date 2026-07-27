# Kubernetes and GKE

## Why

This capability runs Hazelcast members, Management Center, the Simulator
coordinator, and load generators in Kubernetes. It makes topology and failure
tests portable between a managed GKE cluster and a cluster that already exists,
without treating Kubernetes pods as SSH hosts.

## Supported model

Set `provisioner: kubernetes` in `inventory_plan.yaml`. Use
`kubernetes.provider: gke` to create or attach a GKE cluster, or `existing` to use the active
kubeconfig context. Simulator can install or verify a Hazelcast Platform
Operator, deploy Management Center and observability, and run a coordinator pod
with StatefulSet-based agents and load generators.

Hazelcast members are operator-managed. Kubernetes test suites therefore use
load generators as `node_hosts` and the Hazelcast service as `member_hosts`.
Set `simulator.image` to an image containing the Simulator build that the pods
will execute, and set `simulator.loadgenerators.count` for fixed capacity.
The image must support the CPU architecture of the Kubernetes nodes; for the
AMD64 GKE tutorial nodes, publish a `linux/amd64` runtime image. The
[Kubernetes tutorial](../examples/k8s/README.md#6-configure-the-simulator-runtime)
includes the build and manifest verification commands.

## Safe lifecycle

Generated resources carry ownership labels. Attached and `existing` clusters
are not deleted by default, and cleanup refuses to remove resources owned by a
different project. `inventory destroy` is the normal teardown path; use the
image-provided recovery helper only when the tutorial directs you to it.

The concise lifecycle is `inventory apply`, `inventory install k8s`, and
`perftest run`, followed by `inventory destroy`. Run these through
`docker-sim` after completing the shared workspace setup.

Start with the [Kubernetes tutorial](../examples/k8s/README.md). It covers
authentication, capacity checks, project configuration, deployment verification,
runtime controls, and teardown for both GKE and existing clusters.
