# Multi-DC Test Runbook

Run these commands from the repository root. The scenarios use Hazelcast
Enterprise, Management Center, and the optional Prometheus/Grafana observability
host where the selected template supports it.

Set the shared paths once. `./bin/docker-sim` defaults `SIM_IMAGE` to
`hazelcast/simulator:latest`, so setting `SIM_IMAGE` is optional unless you want
to use a local image.

```bash
mkdir -p ./simulator-projects ./mvnrepo
export SIM_IMAGE="${SIM_IMAGE:-hazelcast/simulator:latest}"
```

## Build Local Docker Image

Use a local image when `hazelcast/simulator:latest` does not yet contain the
multi-DC, control, and observability changes from this checkout.

Build the image from the repository root:

```bash
docker build -t hazelcast/simulator:local .
```

Then point the runbook commands at the local image:

```bash
export SIM_IMAGE=hazelcast/simulator:local
```

Verify the image exposes the expected CLIs:

```bash
docker run --rm -it -v "$(pwd):/workspace" "$SIM_IMAGE" perftest --help
docker run --rm -it -v "$(pwd):/workspace" "$SIM_IMAGE" inventory --help
```

## Scope

This runbook covers five manual scenarios:

1. `hazelcast5-ec2` regression test with a normal single-DC setup
2. `hazelcast5-multidc-ec2` single-region multi-DC with 3 members over 2 AZs
3. `hazelcast5-multidc-ec2` two-region multi-DC with 3 members, one in each AZ
4. `hazelcast5-multidc-ec2` node failover during a 10 minute Enterprise test
5. `hazelcast5-multidc-ec2` single-region 3-AZ DC failover with a 2/2/1 deployment

The first three scenarios use the 5 minute Enterprise smoke test from
[smoke-tests.yaml](./smoke-tests.yaml). The failover scenarios use
[enterprise-failover-10m-tests.yaml](./enterprise-failover-10m-tests.yaml).

## Create Simulator Projects

Each scenario starts by creating a Simulator project with `perftest create`,
then copying in the scenario-specific `inventory_plan.yaml` and `tests.yaml`.
The `perftest create` command generates the project directory and its `key` /
`key.pub` pair under `./simulator-projects`.

Because these scenarios use Hazelcast Enterprise, set the license key before
replacing the placeholder in the copied `tests.yaml`:

```bash
export HZ_LICENSEKEY='<your Hazelcast Enterprise license key>'
export PROJECT="$(pwd)/simulator-projects/<project>"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

`./bin/docker-sim` forwards `HZ_LICENSEKEY` into the Simulator container when it
is set. During `inventory install observability`, the same value is applied to
Management Center through `MC_LICENSE` and `-Dhazelcast.mc.license` before MC is
restarted. The value is not passed on the local Ansible command line, but the
Java system property can be visible in the MC JVM process arguments on the
remote host while MC is running.

Before provisioning a managed AWS scenario, make sure AWS credentials are
available in `~/.aws` and that the copied `inventory_plan.yaml` contains valid
AMI, VPC, Internet Gateway, region, and AZ values.

Managed scenarios provision at least:

- Hazelcast members
- 1 load generator
- 1 Management Center host
- 1 observability host with Grafana on port `3000` and Prometheus on port `9090`

The observability installer requires the `mc` group. If the plan has `mc.count:
0` or no `mc` group in `inventory.yaml`, `inventory install observability` should
fail with a clear message instead of producing a partial install.

## Helper Commands

Print AWS values to copy into `inventory_plan.yaml` for one or more regions:

```bash
./bin/aws_inventory_values --team Cloud eu-west-2 eu-central-1
```

Include recent Ubuntu AMI candidates for each region:

```bash
./bin/aws_inventory_values --team Cloud --images eu-west-2 eu-central-1
```

Use the output as:

```yaml
region: <region>
availability_zone: <one availability_zone>
ami: <ami>
vpc_id: <vpc_id>
internet_gateway_id: <internet_gateway_id>
cidr_block: <unused /24 inside the VPC CIDR>
team: <team>
```

Set `PROJECT` to the current project directory before using these helpers:

```bash
export PROJECT="$(pwd)/simulator-projects/<project>"
```

Then run Simulator commands through the Docker wrapper:

```bash
./bin/docker-sim inventory apply
```

Install and verify a provisioned project:

```bash
./bin/docker-sim inventory apply
./bin/docker-sim inventory install java
./bin/docker-sim inventory install simulator
./bin/docker-sim inventory install observability
./bin/docker-sim inventory tune
./bin/docker-sim inventory shell --ping --hosts all
./bin/docker-sim inventory control probe --hosts nodes
```

`inventory install observability` preconfigures MC to connect to the `nodes`
group as cluster `workers`, restarts MC, then starts Prometheus and Grafana. It
also sets `MC_HOME=~/hazelcast-mc` for both `hz-mc conf` and the restarted MC
process, and removes a stale `~/hazelcast-mc/mc.lock` before running `hz-mc
conf`, so rerunning the installer can update MC after an earlier MC process was
stopped.
If you change the cluster name or member port, pass the matching values:

```bash
./bin/docker-sim inventory install observability --member-hosts nodes --member-port 5701 --cluster-name workers
```

Print the MC, Grafana, and Prometheus URLs:

```bash
./bin/docker-sim python3 -c 'import yaml; inv=yaml.safe_load(open("inventory.yaml")); mc=next(iter(inv["mc"]["hosts"])); obs=next(iter(inv["observability"]["hosts"])); print(f"MC: http://{mc}:8080"); print(f"Grafana: http://{obs}:3000"); print(f"Prometheus: http://{obs}:9090")'
```

`inventory install observability` also prints these endpoints at the end of a
successful install.

**Important**: MC is preconfigured during install, but it only connects while the
Hazelcast members are actually running. If you only did inventory apply/install
but have not started a test yet, the cluster may not exist yet.

Toggle member diagnostics dynamically while the cluster is running:

```bash
./bin/docker-sim inventory control diagnostics-status --cluster workers
./bin/docker-sim inventory control diagnostics-on --cluster workers --auto-off-minutes 60
./bin/docker-sim inventory control diagnostics-off --cluster workers
```

These commands use the MC diagnostics configuration REST API. Enterprise MC
licensing and a configured cluster connection are required. The member worker
script preconfigures diagnostics output under each worker directory, so any
diagnostics files generated during the run are downloaded with the normal run
artifacts under each worker's `diagnostics/` directory.

Tail the observability stack:

```bash
./bin/docker-sim inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose ps || sudo docker-compose ps)"
./bin/docker-sim inventory shell --hosts observability "cd ~/hazelcast-observability && (sudo docker compose logs --tail=100 || sudo docker-compose logs --tail=100)"
```

Run a test and inspect the generated report:

```bash
./bin/docker-sim perftest run
./bin/docker-sim sh -lc 'LATEST_RUN=$(find runs -mindepth 2 -maxdepth 2 -type d | sort | tail -1); echo "$LATEST_RUN"; find "$LATEST_RUN/report" -maxdepth 2 -type f | sort'
```

Destroy a project:

```bash
./bin/docker-sim inventory destroy
```

If `inventory apply` fails with duplicate key pair, placement group, or security
group names, AWS already has resources for the same project `basename` and VPC
that are not fully tracked by the current Terraform state. Either destroy the
previous project from the directory that still has its `aws/terraform.tfstate`,
use a new unique `basename` in `inventory_plan.yaml`, or run the manual cleanup
helper:

```bash
./bin/aws_cleanup_project "$PROJECT"
```

Preview the AWS commands without deleting anything:

```bash
./bin/aws_cleanup_project --dry-run "$PROJECT"
```

If `inventory apply` fails with `InvalidSubnet.Conflict`, the configured
`cidr_block` already overlaps an existing subnet in the target VPC. Re-run:

```bash
./bin/aws_inventory_values --team Cloud eu-west-2
```

Then pick an unused `/24` inside the printed VPC CIDR and update
`cidr_block` before applying again.

## Scenario 1: Regression single-DC

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-ec2 regression-single-dc
cp ./examples/multi-dc/regression-single-dc-3nodes.inventory_plan.yaml ./simulator-projects/regression-single-dc/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/regression-single-dc/tests.yaml
export PROJECT="$(pwd)/simulator-projects/regression-single-dc"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

Fill in real values for `basename`, `owner`, `region`, `availability_zone`,
`vpc_id`, `internet_gateway_id`, and role AMIs in `inventory_plan.yaml`.

Provision, install, observe, run, and destroy:

```bash
./bin/docker-sim inventory apply
./bin/docker-sim sh -lc 'cat inventory.yaml'
# Run the helper commands: install and verify, print URLs, run the test, destroy.
```

The regression path uses the older `hazelcast5-ec2` template, which still
launches members and load generators in a `cluster` placement group. AWS does not
support burstable `t*` instances in cluster placement groups, so this example
uses small `c5` sizes for members and load generators.

## Scenario 2: Single-Region Multi-DC Over 2 AZs

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-single-region
cp ./examples/multi-dc/managed-single-region-2az-3nodes.inventory_plan.yaml ./simulator-projects/multidc-single-region/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/multidc-single-region/tests.yaml
export PROJECT="$(pwd)/simulator-projects/multidc-single-region"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

Fill in real values for `basename`, `owner`, `ami`, `vpc_id`, and
`internet_gateway_id`. This example spreads 3 members as:

- `dc-a`: 2 members in `eu-west-2a`
- `dc-b`: 1 member in `eu-west-2b`

Provision, install, observe, run, and destroy:

```bash
./bin/docker-sim inventory apply
./bin/docker-sim sh -lc 'cat inventory.yaml'
# Run the helper commands: install and verify, print URLs, run the test, destroy.
```

## Scenario 3: Two-Region Multi-DC

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-two-region
cp ./examples/multi-dc/managed-two-region-3nodes.inventory_plan.yaml ./simulator-projects/multidc-two-region/inventory_plan.yaml
cp ./examples/multi-dc/smoke-tests.yaml ./simulator-projects/multidc-two-region/tests.yaml
export PROJECT="$(pwd)/simulator-projects/multidc-two-region"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

Fill in real values for `basename`, `owner`, region-specific AMIs if needed, and
the `vpc_id` and `internet_gateway_id` for each region. This example spreads 3
members as:

- `dc-a`: 1 member in `eu-west-2a`
- `dc-b`: 1 member in `eu-west-2b`
- `dc-c`: 1 member in `eu-central-1a`

The load generator, MC, and observability host stay in `dc-a`.

Provision, install, observe, run, and destroy:

```bash
./bin/docker-sim inventory apply
./bin/docker-sim sh -lc 'cat inventory.yaml'
# Run the helper commands: install and verify, print URLs, run the test, destroy.
```

## Scenario 4: Node Failover During a 10 Minute Test

Create a 5-member single-region 3-AZ project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-node-failover
cp ./examples/multi-dc/managed-single-region-3az-5nodes.inventory_plan.yaml ./simulator-projects/multidc-node-failover/inventory_plan.yaml
cp ./examples/multi-dc/enterprise-failover-10m-tests.yaml ./simulator-projects/multidc-node-failover/tests.yaml
export PROJECT="$(pwd)/simulator-projects/multidc-node-failover"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

Fill in real values for `basename`, `owner`, `ami`, `vpc_id`, and
`internet_gateway_id`. The deployment uses 2 members in `dc-a`, 2 members in
`dc-b`, and 1 member in `dc-c`.

Provision and install with the helper commands. Then choose one member host from
`dc-b`:

```bash
export FAILOVER_HOST=$(./bin/docker-sim python3 -c 'import yaml; inv=yaml.safe_load(open("inventory.yaml")); hosts=inv["nodes"]["hosts"]; print(next(host for host,data in hosts.items() if data.get("passthrough:dc") == "dc-b"))')
echo "$FAILOVER_HOST"
```

Start the 10 minute test in one terminal:

```bash
./bin/docker-sim perftest run
```

After the test has been running for at least 60 seconds, restart the selected
member from another terminal:

```bash
./bin/docker-sim inventory control probe --hosts "$FAILOVER_HOST"
./bin/docker-sim inventory control kill-members --hosts "$FAILOVER_HOST" --lapse-seconds 120 --dry-run
./bin/docker-sim inventory control kill-members --hosts "$FAILOVER_HOST" --lapse-seconds 120 --yes
```

The expected result is a completed run with a visible temporary member loss in
MC and Prometheus/Grafana, followed by the member returning before the run ends.

## Scenario 5: Single-Region 3-AZ DC Failover

Create the project:

```bash
docker run --rm -it -v "$(pwd)/simulator-projects:/workspace" -v "$(pwd)/mvnrepo:/root/.m2" "$SIM_IMAGE" perftest create --template hazelcast5-multidc-ec2 multidc-3az-dc-failover
cp ./examples/multi-dc/managed-single-region-3az-5nodes.inventory_plan.yaml ./simulator-projects/multidc-3az-dc-failover/inventory_plan.yaml
cp ./examples/multi-dc/enterprise-failover-10m-tests.yaml ./simulator-projects/multidc-3az-dc-failover/tests.yaml
export PROJECT="$(pwd)/simulator-projects/multidc-3az-dc-failover"
python3 -c 'from pathlib import Path; import os; p = Path(os.environ["PROJECT"]) / "tests.yaml"; p.write_text(p.read_text().replace("<add key here>", os.environ["HZ_LICENSEKEY"]))'
```

Fill in real values for `basename`, `owner`, `ami`, `vpc_id`, and
`internet_gateway_id`, then provision and install with the helper commands.

This scenario fails the singleton DC in the 2/2/1 layout:

- `dc-a`: 2 members in AZ A with the load generator, MC, and observability host
- `dc-b`: 2 members in AZ B
- `dc-c`: 1 member in AZ C

Choose all member hosts in `dc-c`:

```bash
export FAILOVER_HOSTS=$(./bin/docker-sim python3 -c 'import yaml; inv=yaml.safe_load(open("inventory.yaml")); hosts=inv["nodes"]["hosts"]; print(",".join(host for host,data in hosts.items() if data.get("passthrough:dc") == "dc-c"))')
echo "$FAILOVER_HOSTS"
```

Start the 10 minute test in one terminal:

```bash
./bin/docker-sim perftest run
```

After the test has been running for at least 60 seconds, restart the `dc-c`
member from another terminal:

```bash
./bin/docker-sim inventory control probe --hosts "$FAILOVER_HOSTS"
./bin/docker-sim inventory control kill-members --hosts "$FAILOVER_HOSTS" --lapse-seconds 120 --dry-run
./bin/docker-sim inventory control kill-members --hosts "$FAILOVER_HOSTS" --lapse-seconds 120 --yes
```

The expected result is a completed run where the cluster survives the temporary
loss of the one-member DC and converges after `dc-c` rejoins.

## Reports And Logs

`perftest run` automatically downloads worker data and generates a local report.
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
export AMI_OWNER_ID=099720109477
export IMAGE_NAME_PATTERN='ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*'
aws ec2 describe-images \
  --region eu-central-1 \
  --owners "$AMI_OWNER_ID" \
  --filters "Name=name,Values=$IMAGE_NAME_PATTERN" "Name=state,Values=available" \
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
export VPC_ID=vpc-...
aws ec2 describe-internet-gateways \
  --region eu-central-1 \
  --filters Name=attachment.vpc-id,Values="$VPC_ID" \
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
  --filters Name=vpc-id,Values="$VPC_ID" \
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

Set `VPC_ID` from the `Vpc.VpcId` value in the output:

```bash
export VPC_ID=vpc-...
```

Enable DNS support:

```bash
aws ec2 modify-vpc-attribute \
  --region eu-central-1 \
  --vpc-id "$VPC_ID" \
  --enable-dns-support '{"Value":true}'
```

Enable DNS hostnames:

```bash
aws ec2 modify-vpc-attribute \
  --region eu-central-1 \
  --vpc-id "$VPC_ID" \
  --enable-dns-hostnames '{"Value":true}'
```

Create the Internet Gateway:

```bash
aws ec2 create-internet-gateway \
  --region eu-central-1 \
  --tag-specifications 'ResourceType=internet-gateway,Tags=[{Key=Name,Value=simulator-multidc-eu-central-1-igw}]'
```

Set `IGW_ID` from the `InternetGateway.InternetGatewayId` value in the output:

```bash
export IGW_ID=igw-...
```

Attach it to the VPC:

```bash
aws ec2 attach-internet-gateway \
  --region eu-central-1 \
  --internet-gateway-id "$IGW_ID" \
  --vpc-id "$VPC_ID"
```

Verify the VPC:

```bash
aws ec2 describe-vpcs \
  --region eu-central-1 \
  --vpc-ids "$VPC_ID" \
  --query 'Vpcs[].{VpcId:VpcId,Cidr:CidrBlock,Name:Tags[?Key==`Name`]|[0].Value}' \
  --output table
```

Verify the Internet Gateway attachment:

```bash
aws ec2 describe-internet-gateways \
  --region eu-central-1 \
  --internet-gateway-ids "$IGW_ID" \
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

### Manual cleanup for a custom VPC and Internet Gateway

If `inventory destroy` is blocked and you need to remove a custom second-region
VPC manually, use this sequence.

Set the values first:

```bash
export AWS_REGION=eu-central-1
export VPC_ID=vpc-...
export IGW_ID=igw-...
```

Inspect what still exists in the VPC:

```bash
aws ec2 describe-subnets \
  --region "$AWS_REGION" \
  --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'Subnets[].SubnetId' \
  --output table
```

```bash
aws ec2 describe-route-tables \
  --region "$AWS_REGION" \
  --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'RouteTables[].RouteTableId' \
  --output table
```

```bash
aws ec2 describe-security-groups \
  --region "$AWS_REGION" \
  --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[].{GroupId:GroupId,Name:GroupName}' \
  --output table
```

```bash
aws ec2 describe-instances \
  --region "$AWS_REGION" \
  --filters Name=vpc-id,Values="$VPC_ID" Name=instance-state-name,Values=pending,running,stopping,stopped \
  --query 'Reservations[].Instances[].InstanceId' \
  --output table
```

```bash
aws ec2 describe-vpc-peering-connections \
  --region "$AWS_REGION" \
  --query 'VpcPeeringConnections[?AccepterVpcInfo.VpcId==`'"$VPC_ID"'` || RequesterVpcInfo.VpcId==`'"$VPC_ID"'`].VpcPeeringConnectionId' \
  --output table
```

Terminate any remaining instances:

```bash
INSTANCE_IDS=(i-... i-...)
aws ec2 terminate-instances \
  --region "$AWS_REGION" \
  --instance-ids "${INSTANCE_IDS[@]}"
```

```bash
aws ec2 wait instance-terminated \
  --region "$AWS_REGION" \
  --instance-ids "${INSTANCE_IDS[@]}"
```

Delete any VPC peering connections:

```bash
export PCX_ID=pcx-...
aws ec2 delete-vpc-peering-connection \
  --region "$AWS_REGION" \
  --vpc-peering-connection-id "$PCX_ID"
```

Delete non-default security groups:

```bash
export SG_ID=sg-...
aws ec2 delete-security-group \
  --region "$AWS_REGION" \
  --group-id "$SG_ID"
```

Inspect route table associations:

```bash
aws ec2 describe-route-tables \
  --region "$AWS_REGION" \
  --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'RouteTables[].{RouteTableId:RouteTableId,Associations:Associations[*].{Id:RouteTableAssociationId,Main:Main}}' \
  --output json
```

Disassociate each non-main route table association:

```bash
export RTB_ASSOC_ID=rtbassoc-...
aws ec2 disassociate-route-table \
  --region "$AWS_REGION" \
  --association-id "$RTB_ASSOC_ID"
```

Delete each non-main route table:

```bash
export RTB_ID=rtb-...
aws ec2 delete-route-table \
  --region "$AWS_REGION" \
  --route-table-id "$RTB_ID"
```

Delete the subnets:

```bash
export SUBNET_ID=subnet-...
aws ec2 delete-subnet \
  --region "$AWS_REGION" \
  --subnet-id "$SUBNET_ID"
```

Detach and delete the Internet Gateway:

```bash
aws ec2 detach-internet-gateway \
  --region "$AWS_REGION" \
  --internet-gateway-id "$IGW_ID" \
  --vpc-id "$VPC_ID"
```

```bash
aws ec2 delete-internet-gateway \
  --region "$AWS_REGION" \
  --internet-gateway-id "$IGW_ID"
```

Delete the VPC:

```bash
aws ec2 delete-vpc \
  --region "$AWS_REGION" \
  --vpc-id "$VPC_ID"
```
