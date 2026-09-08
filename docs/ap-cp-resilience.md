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

## Failure-tolerant AP map workload

`com.hazelcast.simulator.tests.map.FailureTolerantLongByteArrayMap` is a
native-asynchronous variant of `LongByteArrayMapTest` for member-loss, client
disconnect, operation-timeout, and split-brain-protection experiments. Each
logical operation retains its original map, key, and value while retrying.
Simulator receives the logical operation's future, so latency completion is
associated with successful verification rather than retry submission.

The retry and recovery checks can be configured in the test entry:

```yaml
test:
  - class: com.hazelcast.simulator.tests.map.FailureTolerantLongByteArrayMap
    name: map
    threadCount: 40
    ratePerSecond: 10_000

    keyDomain: 1_000_000
    valueCount: 100
    minValueLength: 1_000
    maxValueLength: 1_000

    getProb: 0.9
    putProb: 0.1

    maxOutstandingOperations: 256
    retryDelayMillis: 100
    retrySchedulerThreadCount: 2
    verifyMapSize: true
    requireSuccessAfterFailure: true
```

### Retry configuration

| Property | Default | Valid values | Behavior and guidance |
| --- | ---: | --- | --- |
| `maxOutstandingOperations` | `256` | `1` or greater | Maximum number of admitted logical operations per workload instance (normally one worker JVM), shared by all timestep threads, maps, and target clients. At capacity, an issuing timestep thread blocks until an operation completes or the test stops; no additional Hazelcast invocation is submitted. An admitted operation keeps its permit while active and between retries, which bounds both in-flight invocations and scheduled retry backlog. Use a lower value with large values or a small client heap. This limit does not include the workload's pre-generated value pool, so configure `valueCount` and value sizes within the heap budget as well. |
| `retryDelayMillis` | `100` | `0` or greater | Delay between a retryable failure and the next attempt of that same logical operation. Use a nonzero value in failure tests to avoid creating a tight retry loop while the cluster is unavailable. Zero is primarily useful for unit tests. This delay does not block a Simulator timestep thread. |
| `retrySchedulerThreadCount` | `2` | `1` or greater | Number of daemon threads which initiate retries after their delay. These threads do not run synchronous map calls. Increase the value only when a large number of outstanding operations causes measurable retry-submission lag after recovery. A logical operation still has no more than one active attempt. |

Negative retry delays, a non-positive outstanding-operation limit, and
non-positive scheduler thread counts fail during test setup. The workload
retries only recognized transient failures; changing these properties does not
make unexpected exceptions retryable.

Backpressure is intentionally applied to the whole logical operation rather
than each individual attempt. Simulator therefore measures admission waiting as
part of operation latency and reports throughput at the sustainable completion
rate. The final workload summary reports issued, completed, retried,
backpressured, peak-outstanding, and currently outstanding operation counts.

For example, `valueCount: 1000` with one-megabyte values retains roughly 1 GB of
generated values in every worker JVM before accounting for Hazelcast client
state and operation payloads. Reduce that static pool and/or
`maxOutstandingOperations` when using multi-megabyte values; the operation
limit cannot recover heap already consumed by generated test data.

### Recovery verification

| Property | Default | Behavior and guidance |
| --- | ---: | --- |
| `verifyMapSize` | `true` | Requires each configured map to contain exactly `keyDomain` entries at verification. Keep this enabled for the normal workload. Disable it only when TTL, eviction, or another intentional mechanism can change the key set. Unexpected failures and outstanding operations remain fatal when this check is disabled. |
| `requireSuccessAfterFailure` | `true` | When at least one retryable failure was observed, requires a logical operation to succeed after the last such failure. This proves that the workload observed recovery. Disable it only for a scenario intentionally ending while the cluster remains unavailable. At least one operation must still have succeeded during the run, and unexpected failures remain fatal. |

The workload always checks that no unexpected asynchronous failure was hidden,
at least one logical operation completed successfully, and no logical operation
remains outstanding. The two verification switches do not weaken those checks.

Operation probabilities, key distribution, value sizes, map count, timestep
thread count, and rate remain standard Simulator properties. `getAll` and
`sizeLog` must remain inactive because Hazelcast does not provide native
asynchronous APIs for those operations. Enabling either operation fails
explicitly instead of moving a synchronous call onto a background pool.
