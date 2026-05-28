# Multi-DC Test Runbook

Run these commands from the repository root.

This runbook assumes your test projects live under:

```bash
./simulator-projects
```

To reuse Maven artifacts across all scenarios, it also assumes a shared local Maven cache directory:

```bash
./mvnrepo
```

Set the simulator image once before running the commands:

```bash
export SIM_IMAGE=hazelcast/simulator:latest
```

This runbook gives you three manual smoke-test scenarios:

1. `hazelcast5-ec2` regression test with a normal single-DC setup
2. `hazelcast5-multidc-ec2` single-region multi-DC with 3 members over 2 AZs
3. `hazelcast5-multidc-ec2` two-region multi-DC with 2 AZs in one region and 1 AZ in a second region

Each scenario uses the 5 minute smoke test from
[smoke-tests.yaml](./smoke-tests.yaml).

## Common prerequisites

- AWS credentials available in `~/.aws` for the managed scenarios
- a valid Ubuntu AMI in each target region
- Docker installed locally
- working `key` and `key.pub` in the created project directory
- a shared Maven cache directory at `./mvnrepo`

Create the shared project and Maven directories once:

```bash
mkdir -p ./simulator-projects ./mvnrepo
```

Managed scenarios provision:
- 3 Hazelcast members total
- 1 load generator
- 1 Management Center

The provided managed examples use these small smoke-test sizes by default:
- nodes: `t3.medium`
- loadgenerators: `t3.small`
- mc: `t3.small`

The regression single-DC example is different:
- nodes: `c5.large`
- loadgenerators: `c5.large`
- mc: `t3.small`

That regression path uses the older `hazelcast5-ec2` template, which still launches members and load generators in a `cluster` placement group. AWS does not support burstable `t*` instances in cluster placement groups, so the regression example intentionally stays on small `c5` sizes.

## Smoke test workflow

For all scenarios, the smoke test workflow is:
- prepare the project files locally
- provision infrastructure only for managed scenarios
- install Java
- install Simulator
- tune the environment
- verify remote reachability
- copy in the 5 minute smoke test
- run `perftest run`

## Scenario 1: Regression single-DC

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-ec2 regression-single-dc
cp ./examples/multi-dc/regression-single-dc-3nodes.inventory_plan.yaml ./simulator-projects/regression-single-dc/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/regression-single-dc/tests.yaml
```

The `inventory_plan.yaml` copy is optional. It is only there to give you a known 3-node regression baseline that matches the other smoke-test examples. You can skip that copy and edit the generated plan manually instead.

Fill in your real values in [inventory_plan.yaml](../../simulator-projects/regression-single-dc/inventory_plan.yaml):
- `basename`
- `owner`
- `region`
- `availability_zone`
- `vpc_id`
- `internet_gateway_id`
- `ami`

Provision and run:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory apply
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'cat inventory.yaml'
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory install java
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory install simulator
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory tune
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory shell --ping --hosts all
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest run
```

Destroy:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/regression-single-dc:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory destroy
```

## Scenario 2: Single-region multi-DC over 2 AZs

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-single-region
cp ./examples/multi-dc/managed-single-region-2az-3nodes.inventory_plan.yaml ./simulator-projects/multidc-single-region/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/multidc-single-region/tests.yaml
```

Fill in your real values in [inventory_plan.yaml](../../simulator-projects/multidc-single-region/inventory_plan.yaml):
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
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory apply
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'cat inventory.yaml'
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory install java
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory install simulator
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory tune
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory shell --ping --hosts all
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest run
```

Destroy:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory destroy
```

## Scenario 3: Two-region multi-DC

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-two-region
cp ./examples/multi-dc/managed-two-region-3nodes.inventory_plan.yaml ./simulator-projects/multidc-two-region/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/multidc-two-region/tests.yaml
```

Fill in your real values in [inventory_plan.yaml](../../simulator-projects/multidc-two-region/inventory_plan.yaml):
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
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory apply
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'cat inventory.yaml'
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory install java
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory install simulator
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory tune
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory shell --ping --hosts all
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest run
```

Destroy:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" -v ~/.aws:/root/.aws "$SIM_IMAGE" inventory destroy
```

## Access Management Center

For the managed scenarios, MC is provisioned in the `mc` group and exposed on port `8080`.

Print the MC URL for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" python3 -c 'import yaml; inventory=yaml.safe_load(open("inventory.yaml")); host=next(iter(inventory["mc"]["hosts"])); print(f"http://{host}:8080")'
```

Print the MC URL for the two-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" python3 -c 'import yaml; inventory=yaml.safe_load(open("inventory.yaml")); host=next(iter(inventory["mc"]["hosts"])); print(f"http://{host}:8080")'
```

Tail MC logs for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory shell --hosts mc "tail -n 200 mc.out"
```

Tail MC logs for the two-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" inventory shell --hosts mc "tail -n 200 mc.out"
```

## Retrieve reports and logs

`perftest run` automatically downloads worker data and generates a local report.

Find the latest run directory for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); echo "$LATEST_RUN"'
```

List report files for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); find "$LATEST_RUN/report" -maxdepth 2 -type f | sort'
```

List downloaded worker files for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); find "$LATEST_RUN" -maxdepth 4 -type f | sort | tail -n 50'
```

Regenerate the report for the single-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-single-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); perftest report -o "$LATEST_RUN/report" "$LATEST_RUN"'
```

Do the same for the two-region project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects/multidc-two-region:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); echo "$LATEST_RUN"; find "$LATEST_RUN/report" -maxdepth 2 -type f | sort; find "$LATEST_RUN" -maxdepth 4 -type f | sort | tail -n 50'
```

The most useful artifacts are usually:
- `runs/<test>/<timestamp>/report/index.html`
- `runs/<test>/<timestamp>/report/report.csv`
- `runs/<test>/<timestamp>/results.yaml`
