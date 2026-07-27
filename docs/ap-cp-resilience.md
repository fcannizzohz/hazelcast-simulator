# AP/CP Resilience Coverage

## Why

AP and CP workloads have different correctness and availability expectations
during a partition or member loss. This capability supplies a Kubernetes
scenario that exercises both: normal AP traffic and CPMap operations are tested
before and during controlled pod-failure and network-isolation experiments.

## Supported model

The single-zone synthetic topology represents three logical regions in one
Kubernetes zone. Its `2-2-1` member distribution is derived from StatefulSet
ordinals, so tests and controls can refer to logical DC groups without requiring
provider-specific zones. Chaos Mesh profiles target those inventory groups for
region-C pod failure, region-C network isolation, and optional inter-region
delay.

Run the healthy AP/CP tests before introducing a failure, then inspect chaos
status and the recorded lifecycle data while the failure tests run. Stop any
active experiment by execution ID before teardown. Chaos lifecycle events are
copied into completed run artifacts for correlation with report data.

Follow the [single-zone AP/CP tutorial](../examples/k8s/README.md#single-zone-synthetic-apcp-example)
for the image-backed project setup, plan configuration, test patterns, failure
sequence, verification, and cleanup.
