# Simulator operational documentation

Use this directory for shared operational guidance. The examples under
`examples/` are runnable, provider-specific procedures; they contain the
commands and configuration needed for their scenario and link here for feature
semantics rather than repeating them.

## Common image-backed workflow

The tutorials run Simulator and cloud tooling from `SIM_IMAGE`, while projects
and the Maven cache remain on the host. Create a workspace once, install the
launcher from the image, then create a project with `tutorial-init`. The full
setup procedure is in [workspace initialization](../examples/README_INIT.md).

For every tutorial, keep the selected image and project in the environment:

```bash
export SIM_IMAGE=hazelcast/simulator:latest
export SIMULATOR_WORKSPACE="$HOME/simulator-workspace"
export PATH="$SIMULATOR_WORKSPACE/bin:$PATH"
export PROJECT="$SIMULATOR_WORKSPACE/projects/<project>"
```

`docker-sim` mounts only projects below `$SIMULATOR_WORKSPACE/projects`; it
also mounts `~/.m2`, `~/.aws`, `~/.config/gcloud`, and `~/.kube`. Do not put
credentials or license keys in project files. Set `HZ_LICENSEKEY` in the shell
when an Enterprise scenario requires it.

## Lifecycle and safety

Run `inventory destroy` from the same project after every tutorial that
provisions resources, including after a failed run. It removes only resources
owned by that project. Existing Kubernetes clusters are retained; a GKE cluster
is deleted only when the project created it. Keep `.simulator-k8s` and the
Terraform state until destroy succeeds.

Before a disruptive control, inspect or render it first and record the Chaos
execution ID. Stop detached experiments before teardown:

```bash
docker-sim inventory control chaos-status
docker-sim inventory control chaos-stop --execution-id <id> --yes
docker-sim inventory destroy
```

## Feature reference

| Feature | Reference | Runnable procedure |
| --- | --- | --- |
| Image-backed workspace | [Image-backed workspaces](image-backed-workspaces.md) | [Workspace initialization](../examples/README_INIT.md) |
| Kubernetes and GKE | [Kubernetes and GKE](kubernetes-gke.md) | [Kubernetes tutorial](../examples/k8s/README.md) |
| AWS multi-DC | [AWS multi-DC](aws-multi-dc.md) | [AWS multi-DC runbook](../examples/multi-dc/README.md) |
| AWS multi-DC implementation | [Implementation reference](multi-dc-implementation.md) | — |
| Cluster controls and Chaos Mesh | [Controls and chaos](cluster-controls-and-chaos.md) | [Kubernetes controls](../examples/k8s/README.md#11-exercise-controls) |
| Observability exports | [Observability](observability.md) | [Kubernetes observability](../examples/k8s/README.md#12-use-observability) |
| AP/CP resilience | [AP/CP resilience](ap-cp-resilience.md) | [AP/CP scenario](../examples/k8s/README.md#single-zone-synthetic-apcp-example) |

The [root README](../README.md) is the general Simulator reference, including
test-suite authoring and reporting.
