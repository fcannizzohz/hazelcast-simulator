# Cluster Controls and Chaos

## Why

`inventory control` provides bounded, inventory-aware fault injection and
runtime inspection. It avoids indiscriminate process cleanup and gives a
Simulator project a consistent way to observe members, restart selected
members, change diagnostics, and run Kubernetes chaos experiments.

## What is supported

Use `probe` to inspect simulator agents and workers. On managed AWS projects,
member lifecycle commands target member workers only: `member_signal`,
`member_restart`, `graceful-restart-members`, and `kill-members`. They require
an explicit host selection and support dry-run and confirmation safeguards.

Diagnostics commands (`diagnostics-status`, `diagnostics-on`, and
`diagnostics-off`) use Management Center. They require an Enterprise-licensed
Management Center connected to the target cluster. Member worker diagnostics are
preconfigured to write into the collected worker artifacts; a custom worker
script must provide equivalent JVM settings.

For Kubernetes inventories, member controls operate on pods. With Chaos Mesh,
`split-brain` and the `chaos-list`, `chaos-render`, `chaos-run`,
`chaos-status`, and `chaos-stop` commands support scoped profiles and validated
manifests. Persistent schedules and elevated scopes require explicit opt-in.

## Using controls safely

Inspect first, render or dry-run before applying a disruptive action, record the
execution ID for asynchronous chaos, and stop active experiments before
destroying the inventory. Controls do not support unmanaged existing-cluster
inventories.

Use the [Kubernetes controls tutorial](../examples/k8s/README.md#11-exercise-controls)
for concrete commands and profile examples.
