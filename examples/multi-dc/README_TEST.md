# Multi-DC Test Runbook

This runbook gives you four manual smoke-test scenarios:

1. `hazelcast5-ec2` regression test with a normal single-DC setup
2. `hazelcast5-multidc-ec2` single-region multi-DC with 3 members over 2 AZs
3. `hazelcast5-multidc-ec2` two-region multi-DC with 2 AZs in one region and 1 AZ in a second region
4. `hazelcast5-existing-cluster` three-region existing cluster with 1 member in each region

Each scenario uses a simple 5 minute smoke test from
[smoke-tests.yaml](/Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml).

## Common prerequisites

- AWS credentials available for the managed scenarios
- a valid Ubuntu AMI in each target region
- working `key` and `key.pub` in the created project directory
- Java and Terraform available locally

Managed scenarios provision:
- 3 Hazelcast members total
- 1 load generator
- 1 Management Center

The existing-cluster scenario assumes:
- the 3-member Hazelcast cluster already exists
- you have at least 1 reachable load generator
- optional existing Management Center is already running if you want MC access there

## Smoke test workflow

Use this same workflow for all scenarios after the project files are prepared.

```bash
inventory install java
inventory install simulator
inventory tune
inventory shell --ping --hosts all
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml tests.yaml
perftest run
```

For the existing-cluster scenario, `inventory shell --ping --hosts all` means the configured load generators only.

## Scenario 1: Regression single-DC

Create a project:

```bash
perftest create --template hazelcast5-ec2 regression-single-dc
cd regression-single-dc
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/regression-single-dc-3nodes.inventory_plan.yaml inventory_plan.yaml
```

Fill in your real values for:
- `basename`
- `owner`
- `region`
- `availability_zone`
- `vpc_id`
- `internet_gateway_id`
- `ami`

Provision and run:

```bash
inventory apply
cat inventory.yaml
inventory install java
inventory install simulator
inventory tune
inventory shell --ping --hosts all
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml tests.yaml
perftest run
```

Destroy:

```bash
inventory destroy
```

## Scenario 2: Single-region multi-DC over 2 AZs

Create a project:

```bash
perftest create --template hazelcast5-multidc-ec2 multidc-single-region
cd multidc-single-region
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/managed-single-region-2az-3nodes.inventory_plan.yaml inventory_plan.yaml
```

Fill in your real values for:
- `basename`
- `owner`
- `ami`
- `vpc_id`
- `internet_gateway_id`

This example spreads 3 members as:
- `dc-a`: 2 members in `eu-west-2a`
- `dc-b`: 1 member in `eu-west-2b`

Provision and run:

```bash
inventory apply
cat inventory.yaml
inventory install java
inventory install simulator
inventory tune
inventory shell --ping --hosts all
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml tests.yaml
perftest run
```

Destroy:

```bash
inventory destroy
```

## Scenario 3: Two-region multi-DC

Create a project:

```bash
perftest create --template hazelcast5-multidc-ec2 multidc-two-region
cd multidc-two-region
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/managed-two-region-3nodes.inventory_plan.yaml inventory_plan.yaml
```

Fill in your real values for:
- `basename`
- `owner`
- region-specific `ami` values if needed
- the `vpc_id` and `internet_gateway_id` for each region

If your AMI IDs differ by region, add per-DC overrides under `dcs[*].nodes.ami`, `dcs[*].loadgenerators.ami`, and `dcs[*].mc.ami` while keeping the top-level role AMIs as defaults.

This example spreads 3 members as:
- `dc-a`: 1 member in `eu-west-2a`
- `dc-b`: 1 member in `eu-west-2b`
- `dc-c`: 1 member in `eu-central-1a`

Provision and run:

```bash
inventory apply
cat inventory.yaml
inventory install java
inventory install simulator
inventory tune
inventory shell --ping --hosts all
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml tests.yaml
perftest run
```

Destroy:

```bash
inventory destroy
```

## Scenario 4: Three-region existing cluster

Use the unchanged `hazelcast5-existing-cluster` template here. It already supports arbitrary cluster layouts without simulator provisioning.

Create a project:

```bash
perftest create --template hazelcast5-existing-cluster multidc-existing-3region
cd multidc-existing-3region
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/existing-cluster-three-region.inventory.yaml inventory.yaml
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/existing-cluster-three-region.client-hazelcast.xml client-hazelcast.xml
cp /Users/fcannizzo/work/github-hz/hazelcast-simulator/examples/multi-dc/smoke-tests.yaml tests.yaml
```

Fill in your real values for:
- the load generator public IP / SSH user / SSH key path in `inventory.yaml`
- the 3 Hazelcast member private addresses and cluster name in `client-hazelcast.xml`

Run:

```bash
inventory install java
inventory install simulator
inventory tune
inventory shell --ping --hosts all
perftest run
```

There is no simulator-managed `inventory destroy` for this scenario because the cluster already exists.

## Access Management Center

For the managed scenarios, MC is provisioned in the `mc` group and exposed on port `8080`.

Print the URL:

```bash
python3 - <<'PY'
import yaml
with open("inventory.yaml") as f:
    inventory = yaml.safe_load(f)
host = next(iter(inventory["mc"]["hosts"]))
print(f"http://{host}:8080")
PY
```

For MC logs:

```bash
inventory shell --hosts mc "tail -n 200 mc.out"
```

For the existing-cluster scenario, use your existing MC endpoint if one already exists. This template does not provision a new MC instance.

## Retrieve reports and logs

`perftest run` automatically downloads worker data and generates a local report.

Find the latest run directory:

```bash
LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1)
echo "$LATEST_RUN"
```

Inspect the generated report files:

```bash
find "$LATEST_RUN/report" -maxdepth 2 -type f | sort
```

The most useful artifacts are usually:
- `$LATEST_RUN/report/index.html`
- `$LATEST_RUN/report/report.csv`
- `$LATEST_RUN/results.yaml`

Inspect downloaded worker files:

```bash
find "$LATEST_RUN" -maxdepth 4 -type f | sort | tail -n 50
```

If you want to regenerate the report manually:

```bash
perftest report -o "$LATEST_RUN/report" "$LATEST_RUN"
```
