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
3. `hazelcast5-multidc-ec2` two-region multi-DC with 3 members, one in each AZ

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

Like the single-region example, the load generator and MC stay in `dc-a` so the only real change is that the third member moves to a second region.

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

## Appendix: Find AWS VPC, IGW, and CIDR values

Use these commands to discover the values you need for:

```yaml
vpc_id: vpc-...
internet_gateway_id: igw-...
cidr_block: 172.31.88.0/24
```

The examples below use `eu-central-1`, but you can replace that region with any other target region.

### Discover a matching AMI in another region

First inspect the AMI you already used successfully in `eu-west-2`:

```bash
aws ec2 describe-images \
  --region eu-west-2 \
  --image-ids ami-03ceeb33c1e4abcd1 \
  --query 'Images[].{ImageId:ImageId,Name:Name,Owner:ImageOwnerAlias,OwnerId:OwnerId,Created:CreationDate,PlatformDetails:PlatformDetails,Description:Description}' \
  --output table
```

Print just the source image name:

```bash
aws ec2 describe-images \
  --region eu-west-2 \
  --image-ids ami-03ceeb33c1e4abcd1 \
  --query 'Images[0].Name' \
  --output text
```

Then search for the same image family in `eu-central-1` by owner and name pattern:

```bash
aws ec2 describe-images \
  --region eu-central-1 \
  --owners <OWNER_ID> \
  --filters "Name=name,Values=<IMAGE_NAME_OR_PATTERN>" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-10:].{ImageId:ImageId,Name:Name,Created:CreationDate}' \
  --output table
```

If the source image is Ubuntu 22.04 from Canonical, this shortcut usually works well:

```bash
aws ec2 describe-images \
  --region eu-central-1 \
  --owners 099720109477 \
  --filters \
    "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
    "Name=architecture,Values=x86_64" \
    "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-10:].{ImageId:ImageId,Name:Name,Created:CreationDate}' \
  --output table
```

Show the default VPC and its CIDR:

```bash
aws ec2 describe-vpcs \
  --region eu-central-1 \
  --filters Name=is-default,Values=true \
  --query 'Vpcs[].{VpcId:VpcId,Cidr:CidrBlock}' \
  --output table
```

Show all VPCs in the region:

```bash
aws ec2 describe-vpcs \
  --region eu-central-1 \
  --query 'Vpcs[].{Name:Tags[?Key==`Name`]|[0].Value,VpcId:VpcId,Cidr:CidrBlock,Default:IsDefault}' \
  --output table
```

Show the Internet Gateway attached to one VPC:

```bash
aws ec2 describe-internet-gateways \
  --region eu-central-1 \
  --filters Name=attachment.vpc-id,Values=<VPC_ID> \
  --query 'InternetGateways[].{IgwId:InternetGatewayId,VpcId:Attachments[0].VpcId}' \
  --output table
```

Show all Internet Gateways in the region:

```bash
aws ec2 describe-internet-gateways \
  --region eu-central-1 \
  --query 'InternetGateways[].{IgwId:InternetGatewayId,VpcId:Attachments[0].VpcId}' \
  --output table
```

Show existing subnets in one VPC so you can choose a free subnet CIDR:

```bash
aws ec2 describe-subnets \
  --region eu-central-1 \
  --filters Name=vpc-id,Values=<VPC_ID> \
  --query 'Subnets[].{SubnetId:SubnetId,Az:AvailabilityZone,Cidr:CidrBlock,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table
```

### Create a custom non-overlapping VPC and Internet Gateway

If the default VPC CIDR overlaps with another region, create a custom VPC. This
example uses `10.50.0.0/16` in `eu-central-1`.

Create the VPC:

```bash
aws ec2 create-vpc \
  --region eu-central-1 \
  --cidr-block 10.50.0.0/16 \
  --tag-specifications 'ResourceType=vpc,Tags=[{Key=Name,Value=simulator-multidc-eu-central-1}]'
```

Enable DNS support:

```bash
aws ec2 modify-vpc-attribute \
  --region eu-central-1 \
  --vpc-id <VPC_ID> \
  --enable-dns-support
```

Enable DNS hostnames:

```bash
aws ec2 modify-vpc-attribute \
  --region eu-central-1 \
  --vpc-id <VPC_ID> \
  --enable-dns-hostnames
```

Create the Internet Gateway:

```bash
aws ec2 create-internet-gateway \
  --region eu-central-1 \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=simulator-multidc-eu-central-1-igw}]'
```

Attach it to the VPC:

```bash
aws ec2 attach-internet-gateway \
  --region eu-central-1 \
  --internet-gateway-id <IGW_ID> \
  --vpc-id <VPC_ID>
```

Verify the VPC:

```bash
aws ec2 describe-vpcs \
  --region eu-central-1 \
  --vpc-ids <VPC_ID> \
  --query 'Vpcs[].{VpcId:VpcId,Cidr:CidrBlock,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table
```

Verify the Internet Gateway attachment:

```bash
aws ec2 describe-internet-gateways \
  --region eu-central-1 \
  --internet-gateway-ids <IGW_ID> \
  --query 'InternetGateways[].{IgwId:InternetGatewayId,VpcId:Attachments[0].VpcId}' \
  --output table
```

Example values to paste for a second-region DC after creating a custom VPC:

```yaml
    - name: dc-c
      region: eu-central-1
      availability_zone: eu-central-1a
      vpc_id: <VPC_ID>
      internet_gateway_id: <IGW_ID>
      cidr_block: 10.50.90.0/24
      nodes:
          count: 1
          ami: <EU_CENTRAL_1_AMI>
      loadgenerators:
          count: 0
```

Practical sequence:

1. Check which AMI family you used in the first region.
2. Find the matching AMI in the second region.
3. Find the VPC you want to reuse, or create a custom non-overlapping VPC.
4. Find or create the attached Internet Gateway for that VPC.
5. List the existing subnets in that VPC.
6. Pick an unused `/24` CIDR inside the VPC CIDR range for each DC.
