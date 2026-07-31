# Testing Hazelcast Simulator on Kubernetes

This tutorial runs Hazelcast members, Management Center, Prometheus, Grafana,
the Simulator coordinator, and every load generator inside Kubernetes. The
Simulator control plane runs from the selected Docker image and transfers run
inputs and results with `kubectl`.

This is an operational procedure. For the supported deployment model,
ownership rules, and feature constraints, see
[Kubernetes and GKE](../../docs/kubernetes-gke.md) and
[cluster controls and chaos](../../docs/cluster-controls-and-chaos.md).

Before following any Kubernetes scenario, complete the shared
[workspace initialization guide](../README_INIT.md). It creates the external
`$SIMULATOR_WORKSPACE/projects` directory, verifies the shared `~/.m2` mount,
and installs the image-supplied `docker-sim` command.

The same Simulator commands are used for AWS and Kubernetes projects. Kubernetes
member lifecycle operations are translated to pod or Chaos Mesh operations, and
Inventory groups continue to represent roles and data centers.

## Supported Environments

| Provider | Use case | Cluster lifecycle |
| --- | --- | --- |
| `gke` | Create or attach to Google Kubernetes Engine | Deletes only a cluster created by this Simulator project |
| `existing` | EKS, AKS, existing GKE, local, or on-premises Kubernetes | Never deletes the cluster |

`existing` is provider-neutral. It uses the selected kubeconfig context and does
not require cloud-specific configuration.

## Deployment Model

The Kubernetes template manages:

- Hazelcast members through Hazelcast Platform Operator.
- Management Center in the workload namespace.
- Prometheus and Grafana with the repository's existing dashboards.
- Optional Chaos Mesh integration for failure and partition controls.
- Persistent Simulator agent pods and one ephemeral coordinator pod per run.
- Inventory groups derived from live pods, services, and topology labels.

The default Kubernetes runtime places the coordinator and agents in the
cluster and uses cluster DNS. Configure that mode with the image and replica
count under `simulator.loadgenerators`.

Kubernetes plans always run the coordinator and load generators as Pods. Do
not configure `loadgenerators.hosts` in this workflow.

## Prerequisites

The selected Simulator image contains:

- Python 3.11 and Simulator dependencies.
- `kubectl` and Helm 3.
- `gcloud` and `gke-gcloud-auth-plugin` for GKE.
- An OCI registry reachable by every cluster node.

You also need a Hazelcast Enterprise license, a Kubernetes account allowed to
create the planned resources, and at least two schedulable topology domains for
the default two-DC example.

Verify the provider tools in the selected image before logging in:

```bash
docker-sim kubectl version --client
docker-sim helm version --short
docker-sim gcloud version
docker-sim gke-gcloud-auth-plugin --version
```

## GKE Tutorial

All `docker-sim` commands below run the Simulator CLI from `SIM_IMAGE`. Set a
project only after initializing it in `$SIMULATOR_WORKSPACE/projects`; the
launcher rejects checkout-relative and other external project paths.

### 1. Authenticate to Google Cloud

```bash
docker-sim gcloud auth login --no-launch-browser
docker-sim gcloud projects list
```

Select a project you can use from the `PROJECT_ID` column:

```bash
export GCP_PROJECT=<project-id>
docker-sim gcloud config set project "$GCP_PROJECT"
```

The GKE provider uses the active `gcloud` credentials and passes the configured
project and location explicitly to cluster commands. Check whether the
Kubernetes Engine API is already enabled:

```bash
docker-sim gcloud services list \
    --enabled \
    --project "$GCP_PROJECT" \
    --filter='config.name:container.googleapis.com'
```

If the command returns `container.googleapis.com`, continue with the tutorial.
If it is not enabled, enable it only when your account has the
`serviceusage.services.enable` permission:

```bash
docker-sim gcloud services enable container.googleapis.com --project "$GCP_PROJECT"
```

If this returns `PERMISSION_DENIED`, the project is still usable if an
administrator enables the API. Ask a project administrator to run the command
or grant the required Service Usage permission. You also need permission to
create GKE clusters and consume project resources.

If no suitable existing project is available, creating one requires
`resourcemanager.projects.create` in an allowed organization or folder and a
billing account must be linked before GKE resources can be created. An
administrator can create and prepare it with:

```bash
docker-sim gcloud projects create PROJECT_ID --name="GKE Simulator Test"
docker-sim gcloud billing projects link PROJECT_ID --billing-account=BILLING_ACCOUNT_ID
docker-sim gcloud services enable container.googleapis.com --project PROJECT_ID
```

Then select it for the tutorial:

```bash
export GCP_PROJECT=PROJECT_ID
docker-sim gcloud config set project "$GCP_PROJECT"
```

Before selecting the GKE values, use the image-bundled helper against the exact
project and region or zone where the cluster will run:

```bash
export GKE_LOCATION=europe-west1
docker-sim gke_inventory_values --project "$GCP_PROJECT" "$GKE_LOCATION"
docker-sim gke_inventory_values --project "$GCP_PROJECT" --machine-types "$GKE_LOCATION"
```

`gke_inventory_values` is the source of truth for the infrastructure choices:
use its active zones for `gke.node_locations` or its zone for `gke.zone`, select
an accessible subnet from the network/subnetwork table, and select a machine
type listed for the zone where nodes will run. Do not copy the example values
below without checking that they exist in your project. The command accepts
multiple locations through `docker-sim`.

### 2. Create a Simulator Project

```bash
docker-sim tutorial-init k8s-smoke my-gke-test
export PROJECT="$SIMULATOR_WORKSPACE/projects/my-gke-test"
```

The generated project contains `inventory_plan.yaml`, `tests.yaml`, the client
configuration, and standard `setup` and `teardown` scripts.

### 3. Configure GKE

Edit `inventory_plan.yaml`:

```yaml
provisioner: kubernetes

kubernetes:
    provider: gke
    instance: my-gke-test
    namespace: simulator
    service_type: ClusterIP
    manage_namespace: true
    delete_namespace_on_destroy: false
    topology_key: topology.kubernetes.io/zone
    wait_timeout_seconds: 600

gke:
    create_cluster: true
    project_id: my-gcp-project       # $GCP_PROJECT
    cluster_name: simulator-k8s
    region: europe-west1             # $GKE_LOCATION, when it is a region
    node_locations:
        - europe-west1-b
        - europe-west1-c
    node_count: 2
    node_machine_type: c2-standard-8
    network: my-vpc                 # select from gke_inventory_values output
    subnetwork: my-subnet           # select a subnet in the chosen region
```

Replace `region` and `node_locations` with the location output from
`gke_inventory_values`. For a zonal cluster, use `zone` instead:

```yaml
gke:
    project_id: my-gcp-project
    cluster_name: simulator-k8s
    zone: europe-west1-b
    node_count: 5
    node_machine_type: n2-standard-8
    network: my-vpc
    subnetwork: my-subnet
```

For a regional GKE cluster, `node_count` is the node count per zone. Review the
resulting cluster size and cost before creating it. The example values are
illustrative; every zone, machine type, network, and subnetwork must come from
the inventory helper output for the selected GCP project and location.

To attach to an existing cluster, use its exact project, cluster, and location
identity and set `create_cluster: false`. Attached clusters are not deleted by
`inventory destroy`.

### 4. Configure Hazelcast and Multi-DC Topology

This example creates four members split evenly between two zones:

```yaml
hazelcast:
    name: workers
    cluster_name: workers
    cluster_size: 4
    repository: docker.io/hazelcast/hazelcast-enterprise
    version: 5.7.0
    license_secret_name: hazelcast-license
    external:
        enabled: false
        type: Smart
        discovery_service_type: LoadBalancer
        member_access: LoadBalancer
    resources:
        requests:
            cpu: "2"
            memory: 8Gi
    pdb:
        enabled: true
        max_unavailable: 1

dcs:
    - name: dc-a
      members: 2
      topology_value: europe-west1-b
    - name: dc-b
      members: 2
      topology_value: europe-west1-c
```

Multi-DC plans require unique names and a total matching
`hazelcast.cluster_size`. Physical-topology plans use unique topology values.
Synthetic logical-region plans may use unequal counts and instead map every
StatefulSet ordinal with `dcs[].pod_ordinals`. Installation fails when live pod
placement does not match the selected plan model.

### 5. Configure the License

The simplest option is an environment variable:

```bash
export HZ_LICENSEKEY="$(< /secure/path/hazelcast-license.txt)"
```

License precedence is:

1. `hazelcast.existing_license_secret`
2. `HZ_LICENSEKEY`
3. `hazelcast.license_file`

To consume an administrator-managed Secret, ensure it has a `license-key` data
entry in the workload namespace and configure:

```yaml
hazelcast:
    existing_license_secret: shared-hazelcast-license
```

Simulator verifies but does not own or delete an existing Secret.

### 6. Configure the Simulator Runtime

The image used by the Kubernetes coordinator and load-generator Pods is
configured separately from the image used by `docker-sim` for the CLI
commands. Use an official Simulator image when it contains the required
version, drivers, and test classes. Build and publish the current checkout only
when you need code or templates that are not in the official image.

For an official image:

```yaml
simulator:
    image: hazelcast/simulator:<version>
    image_pull_policy: IfNotPresent
```

For GKE, build and push a checkout-specific image to a registry reachable by
every cluster node. The GKE configuration in this tutorial uses AMD64
`e2-standard-8` nodes, so publish a `linux/amd64` image. On an Apple Silicon
host, a plain `docker build` produces an ARM image and GKE agents fail with
`ImagePullBackOff` and `no match for platform in manifest`.

```bash
export RUNTIME_SIM_IMAGE=europe-west1-docker.pkg.dev/$GCP_PROJECT/simulator/simulator:dev
docker buildx build --platform linux/amd64 --push -t "$RUNTIME_SIM_IMAGE" .
docker buildx imagetools inspect "$RUNTIME_SIM_IMAGE"
```

Before continuing, confirm the inspection output includes
`Platform: linux/amd64`. The CLI image used by `docker-sim` may remain native to
your workstation; only the `simulator.image` used by Kubernetes Pods must match
the node architecture.

```yaml
simulator:
    image: <selected-image-from-above>
    image_pull_policy: IfNotPresent
```

For a local `kind` cluster, the image can remain local:

```bash
docker build -t hazelcast-simulator:k8s-local .
kind load docker-image hazelcast-simulator:k8s-local
```

```yaml
simulator:
    image: hazelcast-simulator:k8s-local
    image_pull_policy: IfNotPresent
```

```yaml
simulator:
    image: <selected-image-from-above>
    image_pull_policy: IfNotPresent
    image_pull_secrets: []
    coordinator:
        active_deadline_seconds: 86400
        retain_on_failure: false
        resources:
            requests: {cpu: "1", memory: 2Gi}
    loadgenerators:
        count: 2
        reset_before_run: true
        resources:
            requests: {cpu: "2", memory: 4Gi}
        scheduling: {}
```

Use `image_pull_secrets` for a private registry. The StatefulSet spreads agents
across `kubernetes.topology_key` unless explicit scheduling is supplied.

### 7. Configure Operator and Chaos Mesh

Install a pinned Operator release:

```yaml
operator:
    install: true
    version: 5.17.0
    namespace: hz-system
    release_name: simulator-hazelcast-operator
```

Set `operator.install: false` to consume an administrator-managed compatible
Operator. Required CRDs are verified in either mode.

Chaos Mesh also supports install-or-consume behavior:

```yaml
chaosmesh:
    enabled: true
    install: true
    version: 2.8.3
    namespace: chaos-mesh
    release_name: chaos-mesh
    runtime: containerd
    socket_path: /run/containerd/containerd.sock
```

Set `install: false` to consume an existing installation. With
`enabled: false`, member kills fall back to pod deletion and split-brain is not
available.

### 8. Provision and Install

Use the normal Simulator workflow:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
```

`inventory apply` creates or attaches to GKE, gets credentials, verifies API
access, and writes the initial inventory. `inventory install k8s` installs or
verifies dependencies, applies owned resources, waits for member and Simulator
pods, and rewrites `inventory.yaml` with cluster-private addresses.

### 9. Verify the Deployment

Inspect Kubernetes:

```bash
docker-sim kubectl get pods,statefulsets,services -n simulator -o wide
docker-sim kubectl get hazelcast,managementcenter -n simulator
docker-sim kubectl get nodes -L topology.kubernetes.io/zone
```

Inspect Simulator's provider-neutral view:

```bash
docker-sim ansible-inventory -i inventory.yaml --graph
docker-sim inventory control probe --hosts nodes
```

Expected groups include:

- `nodes`: live Hazelcast pods.
- `dc-a`, `dc-b`: pods grouped by topology.
- `hazelcast`: client endpoint.
- `mc` and `observability`: Management Center and Grafana endpoints.
- `loadgenerators` and `simulator_agents`: stable StatefulSet pod DNS names.

### 10. Run a Test

The template uses Kubernetes-managed members and Simulator-managed clients:

```yaml
node_count: 0
loadgenerator_hosts: loadgenerators
member_hosts: hazelcast
```

Do not set `node_hosts` when `node_count: 0`: Hazelcast members are managed by
Kubernetes and do not run Simulator agents. Client workers run only through
`loadgenerator_hosts`, while `member_hosts: hazelcast` resolves the in-cluster
Hazelcast Service.

Run:

```bash
docker-sim perftest run
```

The CLI acquires a ConfigMap run lock, creates a coordinator Pod, stages
inventory and test files, streams coordinator output, retrieves coordinator and
agent results, and removes the Pod. A failed Pod is retained only when
`simulator.coordinator.retain_on_failure: true`.

### 11. Exercise Controls

Inspect destructive actions with `--dry-run` first:

```bash
docker-sim inventory control probe --hosts nodes
docker-sim inventory control kill-members \
  --hosts dc-a --lapse-seconds 30 --start-spread-seconds 10 --dry-run
docker-sim inventory control kill-members \
  --hosts dc-a --lapse-seconds 30 --start-spread-seconds 10 --yes
docker-sim inventory control graceful-restart-members \
  --hosts workers-0 --lapse-seconds 10 --yes
docker-sim inventory control split-brain \
  --partitions dc-a/dc-b --lapse-seconds 60 --dry-run
docker-sim inventory control split-brain \
  --partitions dc-a/dc-b --lapse-seconds 60 --yes
```

With Chaos Mesh, a positive kill lapse uses `pod-failure`; a zero lapse uses
`pod-kill`. Split-brain supports exactly two non-overlapping inventory groups
and always removes its temporary `NetworkChaos` resource.

These controls are built-in profiles and remain available when no custom
profiles are configured. They are reserved and cannot be overridden. Custom
profiles are an additive Chaos Mesh layer:

```yaml
chaosmesh:
    enabled: true
    default_duration: 5m
    allow_elevated_scope: false
    profiles:
        cross-dc-delay:
            kind: NetworkChaos
            targets: dc-a
            target: {targets: dc-b, mode: all}
            mode: all
            duration: 2m
            scope: workload
            spec:
                action: delay
                direction: both
                delay:
                    latency: 100ms
                    jitter: 20ms
                    correlation: "25"
        lossy-clients:
            kind: NetworkChaos
            targets: loadgenerators
            mode: all
            duration: 90s
            scope: workload
            spec:
                action: loss
                loss: {loss: "5", correlation: "20"}
        rolling-failures:
            manifest: chaos/rolling-failures.yaml
            scope: workload
        recurring-client-delay:
            manifest: chaos/recurring-client-delay.yaml
            scope: workload
            persistent: true
```

Render and execute profiles without learning provider-specific DC names:

```bash
docker-sim inventory control chaos-list
docker-sim inventory control chaos-render --profile cross-dc-delay
docker-sim inventory control chaos-run --profile cross-dc-delay --dry-run
docker-sim inventory control chaos-run --profile cross-dc-delay --yes
docker-sim inventory control chaos-status
docker-sim inventory control chaos-stop --execution-id <id> --yes
docker-sim inventory control chaos-run --profile recurring-client-delay --detach --yes
```

Structured `NetworkChaos` profiles accept the full Chaos Mesh delay, jitter,
reorder, loss, duplication, corruption, bandwidth, partition, direction, and
target configuration under `spec`. Raw manifests support any installed
`chaos-mesh.org` resource, including StressChaos, IOChaos, DNSChaos, HTTPChaos,
TimeChaos, KernelChaos, JVMChaos, Workflow, and Schedule. Raw YAML can use
`${WORKLOAD_NAMESPACE}`, `${CHAOS_NAMESPACE}`, `${SIMULATOR_INSTANCE}`, and
`${HAZELCAST_RESOURCE}` placeholders.

Workload scope is namespace restricted. Cluster or cloud scope requires both
`chaosmesh.allow_elevated_scope: true` and `--allow-elevated`; persistent
Schedules also require `persistent: true`, `--detach`, and `--yes`.

The existing latency UX dispatches to NetworkChaos on Kubernetes:

```bash
docker-sim inventory inject_latencies --hosts dc-a --target-hosts dc-b \
  --latency 100 --jitter 20 --correlation 25 --duration 2m --dry-run
docker-sim inventory inject_latencies --hosts dc-a --target-hosts dc-b \
  --latency 100 --jitter 20 --correlation 25 --duration 2m --yes
docker-sim inventory clear_latencies --execution-id <id> --yes
```

## Three-Node Chaos Mesh Smoke Test

The [`smoke-3node`](smoke-3node) example is the smallest end-to-end
Kubernetes check. It deploys three Hazelcast members in one logical DC, one
in-cluster load generator, and a 60-second verified map test. Chaos Mesh adds
2 ms latency with 2 ms jitter in both directions between the member pods.

Initialize the image-bundled smoke scenario and set the Simulator image and
license source in `inventory_plan.yaml`:

```bash
docker-sim tutorial-init k8s-smoke k8s-smoke
export PROJECT="$SIMULATOR_WORKSPACE/projects/k8s-smoke"
```

The plan installs Chaos Mesh because `chaosmesh.install` is true. Set it to
false only when the cluster administrator already manages Chaos Mesh. The
`dcs` entry has no physical topology value, so the smoke test is suitable for a
single-zone cluster or an existing Kubernetes cluster. For an existing
cluster, set `kubernetes.context` and ensure it has capacity for the three
members and one load-generator pod.

Run the smoke test:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
docker-sim inventory import
docker-sim inventory control probe --hosts nodes
docker-sim inventory control chaos-run --profile smoke-member-delay --detach --yes
docker-sim perftest run tests.yaml --pattern k8s-3node-latency-smoke
docker-sim inventory control chaos-status
docker-sim inventory control chaos-stop --execution-id <id> --yes
```

The smoke test passes only when all three members and the load generator become
Ready, the Chaos Mesh resource is created and tracked, the coordinator
completes the verified map test, and the results are retrieved. Inspect
resources with `kubectl get pods -n simulator-smoke` and use the execution ID
from `chaos-status` for cleanup. Finish with:

```bash
docker-sim inventory destroy
```

The image-only tutorial does not use the checkout-only `kind` runner. Use the
GKE or existing-cluster paths in this guide so every Simulator and Kubernetes
operation remains image-backed.

## Same-DC 3-Node Latency and Jitter

This scenario keeps all three Hazelcast members in one physical Kubernetes
topology domain and injects bidirectional latency and jitter between the
members. It tests application behavior under degraded east-west networking
without introducing a multi-DC topology.

Use a three-member plan with one DC and one `topology_value`:

```yaml
hazelcast:
    name: workers
    cluster_name: workers
    cluster_size: 3
    version: 5.7.0
    persistence:
        enabled: false

dcs:
    - name: dc-a
      members: 3
      topology_value: europe-west1-b

chaosmesh:
    enabled: true
    profiles:
        same-dc-delay:
            kind: NetworkChaos
            targets: nodes
            target:
                targets: nodes
                mode: all
            mode: all
            duration: 10m
            scope: workload
            spec:
                action: delay
                direction: both
                delay:
                    latency: 100ms
                    jitter: 25ms
                    correlation: "25"
```

The `nodes` selectors are resolved from the live Hazelcast pod inventory. The
profile is independent of member failure and can be run while the normal test
is active:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
docker-sim inventory control probe --hosts nodes
docker-sim inventory control chaos-run --profile same-dc-delay --detach --yes
docker-sim perftest run --pattern <test-pattern>
docker-sim inventory control chaos-status
docker-sim inventory control chaos-stop --execution-id <id> --yes
```

Adjust `latency`, `jitter`, `correlation`, and `duration` for the experiment.
The delay is managed only by Chaos Mesh; no GKE zone or Linux traffic-shaping
configuration is involved.

## Rolling Hazelcast Upgrades

Rolling upgrades are performed by the Hazelcast Platform Operator. The
Simulator keeps the inventory and test UX unchanged: update the member image
version in `inventory_plan.yaml`, reconcile the Kubernetes installation, wait
for every member to become ready, and then run the test.

Before the upgrade, use a disruption budget and record the current state:

```yaml
hazelcast:
    cluster_size: 3
    version: 5.7.0
    pdb:
        enabled: true
        max_unavailable: 1
```

Capture the baseline and update only to an Operator-supported compatible
version:

```bash
docker-sim inventory control probe --hosts nodes
docker-sim kubectl get hazelcast workers -n simulator -o yaml > before-upgrade.yaml
docker-sim kubectl get pods -n simulator -l app.kubernetes.io/instance=workers -o wide

# Edit hazelcast.version in inventory_plan.yaml, then reconcile:
docker-sim inventory install k8s
docker-sim kubectl rollout status statefulset/workers -n simulator --timeout=15m
docker-sim inventory import
docker-sim inventory control probe --hosts nodes
```

The Operator performs the member replacement according to its reconciliation
and upgrade behavior. `max_unavailable: 1` limits voluntary disruption, but it
does not replace compatibility checks, backups, or CP persistence planning.
Do not combine a version upgrade with an active partition or failure unless
that interaction is the explicit test objective. For a rolling restart
rehearsal without changing the image version, use:

```bash
docker-sim inventory control graceful-restart-members \
    --hosts nodes --lapse-seconds 30 --start-spread-seconds 60 --yes
```

## Three-DC 3-2-2 Failure Test

This scenario uses seven members across three physical Kubernetes topology
domains: three in `dc-a`, two in `dc-b`, and two in `dc-c`. It is useful for
testing loss and recovery of one complete DC while retaining a five-member
cluster.

Configure the topology using values returned by `gke_inventory_values`:

```yaml
gke:
    region: europe-west1
    node_locations:
        - europe-west1-b
        - europe-west1-c
        - europe-west1-d

hazelcast:
    name: workers
    cluster_name: workers
    cluster_size: 7
    version: 5.7.0

dcs:
    - name: dc-a
      members: 3
      topology_value: europe-west1-b
    - name: dc-b
      members: 2
      topology_value: europe-west1-c
    - name: dc-c
      members: 2
      topology_value: europe-west1-d
```

The topology values are examples only; replace them with three schedulable
locations from the target project. The member counts must sum to seven, and
the Kubernetes scheduler must be able to place the requested number of pods in
each location.

For a complete DC failure, define a temporary Chaos Mesh `PodChaos` profile
targeting the inventory group for that DC:

```yaml
chaosmesh:
    enabled: true
    profiles:
        fail-dc-c:
            kind: PodChaos
            targets: dc-c
            mode: all
            duration: 90s
            scope: workload
            spec:
                action: pod-failure
```

Run the healthy test first, then inject the failure and run the failure test:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
docker-sim perftest run --pattern <healthy-test-pattern>

docker-sim inventory control chaos-run --profile fail-dc-c --detach --yes
docker-sim perftest run --pattern <dc-failure-test-pattern>
docker-sim inventory control chaos-status
docker-sim kubectl get pods -n simulator -l app.kubernetes.io/instance=workers -o wide
```

`dc-c` has two members and is recreated after the 90-second failure window.
Use `chaos-stop` with the recorded execution ID to end the experiment early.
For a network-level DC outage that leaves the pods running, replace the
`PodChaos` profile with a bidirectional `NetworkChaos` partition targeting
`dc-c` and targeting `dc-a,dc-b`. Both variants use the same inventory groups,
so controls and tests do not need provider-specific zone names.

Keep this physical 3-2-2 example separate from the synthetic single-zone
logical-region example below. The former tests actual topology-domain loss;
the latter models regions in one zone and uses Chaos Mesh to create the
logical failures.

## Single-Zone Synthetic AP/CP Example

The `single-zone-ap-cp` example models three logical regions in one GKE zone.
It deliberately has no physical multi-zone or provider-level latency. Logical
region membership is defined by stable Hazelcast StatefulSet ordinals:

| Logical region | Hazelcast pods | StatefulSet ordinals |
| --- | --- | --- |
| `region-a` | 2 | `workers-0`, `workers-1` |
| `region-b` | 2 | `workers-2`, `workers-3` |
| `region-c` | 1 | `workers-4` |

Initialize the image-bundled single-zone AP/CP scenario:

```bash
docker-sim tutorial-init k8s-ap-cp single-zone-ap-cp
export PROJECT="$SIMULATOR_WORKSPACE/projects/single-zone-ap-cp"
```

Set the GKE project, simulator image, license source, and storage class in
`inventory_plan.yaml`. The plan uses a zonal GKE cluster, so `gke.zone` is set
and `gke.region` is intentionally absent. All pods remain in that zone;
anti-affinity only spreads Hazelcast members across nodes within the zone.

The `hazelcast.yaml` file is the complete member behavior configuration. It
contains the five CP members, CP group size, CP persistence settings, CPMap
definitions, AP map defaults, and map backup settings. The Operator CR only
injects this file and provisions the persistence PVCs; CP groups and map
behavior are not duplicated in `inventory_plan.yaml`.

Hazelcast CP persistence requires Kubernetes storage. Configure
`hazelcast.persistence` for the PVC size, access mode, and storage class, and
keep `hazelcast.cp_enabled: true`. Do not resize this cluster after CP
initialization.

Apply the project and verify the synthetic topology:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
docker-sim inventory import
docker-sim inventory control probe --hosts nodes
docker-sim kubectl get pods -n simulator -l app.kubernetes.io/instance=workers -o wide
```

Run the healthy AP/CP workload:

```bash
docker-sim perftest run tests-ap-cp.yaml --pattern ap-cp-happy
```

The test runs `StringStringMapTest` and `CPMapTest` together. CPMap uses five
configured CP groups and five configured maps, matching the map configuration
in `hazelcast.yaml`.

Run the temporary logical-region pod failure:

```bash
docker-sim inventory control chaos-run --profile region-c-pod-failure --detach --yes
docker-sim perftest run tests-ap-cp.yaml --pattern ap-cp-region-c-pod-failure
docker-sim inventory control chaos-status
docker-sim kubectl get pods -n simulator -l app.kubernetes.io/instance=workers -o wide
```

`region-c` contains one member, so this fails the complete synthetic region.
The remaining four CP members retain a majority while Kubernetes recreates the
failed pod.

Run the network-isolation scenario separately:

```bash
docker-sim inventory control chaos-run --profile region-c-network-isolation --detach --yes
docker-sim perftest run tests-ap-cp.yaml --pattern ap-cp-region-c-network-isolation
docker-sim inventory control chaos-status
```

This keeps `workers-4` running but partitions it from `region-a` and
`region-b`. The AP/CP test exercises recovery after the Chaos Mesh resource
expires. Inspect lifecycle records in `.simulator-k8s/chaos-events.jsonl` and
the copied `chaos-events.jsonl` in the run directory.

Stop a detached experiment explicitly when needed:

```bash
docker-sim inventory control chaos-list
docker-sim inventory control chaos-stop --execution-id <id> --yes
```

Only Chaos Mesh injects latency, jitter, pod failure, or network isolation in
this example. The three pairwise delay profiles are ready to run concurrently:

```bash
docker-sim inventory control chaos-run --profile region-a-region-b-delay --detach --yes
docker-sim inventory control chaos-run --profile region-a-region-c-delay --detach --yes
docker-sim inventory control chaos-run --profile region-b-region-c-delay --detach --yes
```

Stop all three executions after the workload or use their configured ten-minute
duration. Finish with the normal ownership-aware teardown:

```bash
docker-sim inventory destroy
```

Management Center diagnostics use the same commands as AWS:

```bash
docker-sim inventory control diagnostics-status --cluster workers
docker-sim inventory control diagnostics-on --cluster workers --auto-off-minutes 60
docker-sim inventory control diagnostics-off --cluster workers
```

### 12. Use Observability

The three-node smoke fixture enables Management Center, Prometheus, and Grafana
by default. `inventory install k8s` waits for all three workloads and verifies
that Prometheus reports the Management Center (`job=hazelcast-mc`) scrape target
as `up`; installation fails if the metrics path is unavailable.

Grafana is provisioned with the repository dashboards and a Prometheus
datasource. Cluster-private services can be inspected with temporary port
forwards:

```bash
docker-sim kubectl -n simulator port-forward service/management-center 8080:8080
docker-sim kubectl -n simulator port-forward service/grafana 3000:3000
```

Inspect the scrape path directly when diagnosing a failure:

```bash
docker-sim kubectl -n simulator port-forward service/management-center 8080:8080
curl -fsS http://127.0.0.1:8080/metrics | head
docker-sim kubectl -n simulator port-forward service/prometheus 9090:9090
curl -fsS 'http://127.0.0.1:9090/api/v1/targets?state=active'
```

After a run, export the observability data before tearing down the cluster:

```bash
docker-sim perftest export_observability runs/<test>/<run-timestamp>
```

The export discovers every reportable run below `runs/`, creates a separate
Simulator dashboard for each run, and includes the Prometheus TSDB snapshot,
run artifacts, and dashboards in a portable Docker Compose bundle. Start it
with `docker compose up -d` and open Grafana on port 3000 for offline analysis.

Chaos lifecycle events are appended to `.simulator-k8s/chaos-events.jsonl` and
copied into each completed run as `chaos-events.jsonl` for chart correlation.
When Grafana is enabled, lifecycle transitions are also posted as Grafana
annotations through a temporary port forward.

Refresh endpoints after a service change:

```bash
docker-sim inventory import
docker-sim kubectl get svc management-center grafana -n simulator
```

### 13. Tear Down

```bash
docker-sim inventory destroy
```

Teardown is conservative:

- Resources are deleted only when ownership labels match this project.
- Attached GKE and existing Kubernetes clusters are retained.
- A GKE cluster created by this project is deleted.
- Namespaces, Operator, and Chaos Mesh are retained by default.
- Cleanup failures preserve `.simulator-k8s` state for retry.

Do not delete `.simulator-k8s` before teardown. Enable these options only for
dedicated installations:

```yaml
kubernetes:
    delete_namespace_on_destroy: true
operator:
    uninstall_on_destroy: true
chaosmesh:
    uninstall_on_destroy: true
```

Coordinator Pods, agent StatefulSets, Services, RBAC, and owned chaos resources
are removed with the inventory. Registry images and attached clusters remain.

## Existing Kubernetes Clusters

Use `provider: existing` for EKS, AKS, existing GKE, local, or on-premises
Kubernetes:

```bash
docker-sim kubectl config get-contexts
docker-sim kubectl config use-context my-cluster-context
docker-sim kubectl version
```

Configure:

```yaml
provisioner: kubernetes

kubernetes:
    provider: existing
    instance: my-k8s-test
    namespace: simulator
    context: my-cluster-context
    service_type: ClusterIP
    topology_key: topology.kubernetes.io/zone

operator:
    install: false

chaosmesh:
    enabled: false
```

Set `install: true` only when Simulator should manage that Helm release. Then
run the same workflow:

```bash
docker-sim inventory apply
docker-sim inventory install k8s
docker-sim perftest run
```

The existing provider verifies API access and never creates or deletes the target
cluster.

## Endpoint Options

### LoadBalancer

External exposure is optional because all Simulator workloads run inside the
cluster. Enable it only for clients outside Kubernetes:

```yaml
kubernetes:
    service_type: LoadBalancer
hazelcast:
    external:
        enabled: true
        discovery_service_type: LoadBalancer
        member_access: LoadBalancer
```

The global setting controls Management Center and Grafana. Hazelcast TCP
exposure is configured separately.

### NodePort

NodePort resolves the allocated service port and a node `ExternalIP` or
`ExternalDNS`:

```yaml
kubernetes:
    service_type: NodePort
    node_address: 203.0.113.20
hazelcast:
    external:
        discovery_service_type: NodePort
        member_access: NodePortExternalIP
```

For privately routed load generators, `allow_node_internal_ip: true` permits
InternalIP fallback.

### Explicit Endpoints

Use overrides for private load balancers, gateways, forwarded ports, or custom
DNS:

```yaml
kubernetes:
    endpoints:
        hazelcast:
            host: hz.example.net
            port: 5701
        management_center:
            host: mc.example.net
            port: 443
            scheme: https
        grafana:
            host: grafana.example.net
            port: 443
            scheme: https
```

## Persistence

Use an existing storage class on shared clusters:

```yaml
hazelcast:
    persistence:
        enabled: true
        storage_class_name: premium-rwo
        request_storage: 50Gi
        access_modes: [ReadWriteOnce]
```

The plan can define an owned StorageClass through
`hazelcast.persistence.storage_class`, but an administrator-managed class is
usually safer on shared environments.

## Troubleshooting

### Kubernetes API is unreachable

```bash
docker-sim kubectl config current-context
docker-sim kubectl --context my-cluster-context version
docker-sim gcloud container clusters get-credentials simulator-k8s \
  --project "$GCP_PROJECT" --region europe-west1
```

### LoadBalancer endpoint remains pending

```bash
docker-sim kubectl get svc -n simulator
docker-sim kubectl get events -n simulator --sort-by=.lastTimestamp
```

Confirm the cluster supports external load balancers, or use NodePort or
explicit endpoint overrides.

### Simulator agents are stuck in `ImagePullBackOff`

```bash
docker-sim kubectl get pods -n simulator -o wide
docker-sim kubectl get events -n simulator --sort-by=.lastTimestamp
```

If the events contain `no match for platform in manifest`, the registry image
does not support the node CPU architecture. For the AMD64 GKE nodes used in
this tutorial, rebuild and push the runtime image with:

```bash
docker buildx build --platform linux/amd64 --push -t "$RUNTIME_SIM_IMAGE" .
docker buildx imagetools inspect "$RUNTIME_SIM_IMAGE"
```

Confirm the manifest lists `Platform: linux/amd64`, then rerun
`docker-sim inventory install k8s`. It reuses the existing cluster and retries
the agent Pods. For other image-pull errors, use the event message to check the
image name, registry credentials, and node registry access.

### Topology verification fails

```bash
docker-sim kubectl get nodes -L topology.kubernetes.io/zone
docker-sim kubectl get pods -n simulator -o wide
```

Make `dcs[].topology_value` match node labels exactly and ensure each topology
domain has enough capacity.

### License Secret validation fails

```bash
docker-sim kubectl get secret shared-hazelcast-license -n simulator \
  -o jsonpath='{.data.license-key}'
```

The command prints encoded data. Do not place license contents in logs or
committed files.

### Chaos controls are unavailable

```bash
docker-sim kubectl get crd podchaos.chaos-mesh.org networkchaos.chaos-mesh.org
docker-sim kubectl get pods -n chaos-mesh
```

Kill controls can fall back to pod deletion, but split-brain requires Chaos
Mesh.

Render custom profiles before applying them and verify every required CRD:

```bash
docker-sim inventory control chaos-render --profile cross-dc-delay
docker-sim kubectl get crd | grep chaos-mesh.org
docker-sim inventory control chaos-status
```

### Coordinator or load generators are not ready

```bash
docker-sim kubectl get statefulset,pods -n simulator -l simulator.hazelcast.com/managed=true
docker-sim kubectl logs -n simulator test-k8s-agents-0
docker-sim kubectl get configmap -n simulator -l simulator.hazelcast.com/managed=true
```

An existing `*-run-lock` ConfigMap means a run is active or the local process
ended before cleanup. Confirm no coordinator Pod is running before deleting a
stale lock. For image failures, inspect `kubectl describe pod` and verify
`simulator.image_pull_secrets`.

### Teardown stops after a deletion error

This is intentional. Correct API access, RBAC, or finalizers and retry:

```bash
docker-sim inventory destroy
```

If normal teardown cannot finish, use the recovery cleanup command from the
Simulator repository:

```bash
docker-sim gke_cleanup_project --dry-run /workspace
docker-sim gke_cleanup_project /workspace
```

The cleanup command selects in-cluster resources by the Simulator ownership and
instance labels, including custom Chaos Mesh resource kinds discovered from the
cluster. It deletes the GKE cluster only when
`.simulator-k8s/provider-state.yaml` proves that this project created it. An
attached cluster remains intact unless `gke.delete_existing_cluster: true` is
set explicitly. Namespace and managed add-on removal continue to honor their
respective `*_on_destroy` plan settings.

## Related Documentation

- [Operational documentation](../../docs/README.md): shared workspace and cleanup rules.
- [Kubernetes and GKE](../../docs/kubernetes-gke.md): deployment and ownership model.
- [Cluster controls and chaos](../../docs/cluster-controls-and-chaos.md): control behavior.
- [Observability and portable reports](../../docs/observability.md): dashboards and diagnostics.
- [`templates/hazelcast5-k8s-chaosmesh/README.md`](../../templates/hazelcast5-k8s-chaosmesh/README.md): template reference.
