# Multi-DC Implementation Summary

This summary describes the multi-DC work on the current branch compared with the base branch in this repository, `master`.

The implementation was kept additive:
- existing templates were left in place
- `existing-cluster` behavior was not changed
- the new functionality is centered on one new managed template plus the minimum importer support needed to consume its Terraform outputs

## Existing Files Changed

### [src/simulator/inventory_terraform.py](./src/simulator/inventory_terraform.py)

This is the main code change outside the new template.

What changed:
- Terraform inventory import now accepts multiple output shapes:
  - legacy nested list outputs
  - flat list outputs
  - map/object outputs keyed by instance name
- non-load-balancer hosts now use `public_ip` when present, and fall back to `private_ip`
- imported host data keeps `private_ip` and also preserves `public_ip` when present
- `boto3` import was moved inside the NLB helper so Terraform inventory import does not require `boto3` unless load balancer lookup is actually used

Why it changed:
- the new multi-DC Terraform template emits flat or map-shaped instance collections rather than the single older nested shape
- simulator still needs to end up with the same flat `inventory.yaml` model

### [.gitignore](./.gitignore)

What changed:
- added `mvnrepo/`

Why it changed:
- the new test runbook uses a shared local Maven cache directory mounted into Docker so repeated smoke tests do not keep downloading dependencies

## New Managed Multi-DC Template

A new additive template was added:

- [templates/hazelcast5-multidc-ec2](./templates/hazelcast5-multidc-ec2)

Purpose:
- provide a managed AWS multi-DC deployment path without modifying the existing single-DC templates

Added files:
- [templates/hazelcast5-multidc-ec2/inventory_plan.yaml](./templates/hazelcast5-multidc-ec2/inventory_plan.yaml)
- [templates/hazelcast5-multidc-ec2/README.md](./templates/hazelcast5-multidc-ec2/README.md)
- [templates/hazelcast5-multidc-ec2/aws/main.tf](./templates/hazelcast5-multidc-ec2/aws/main.tf)
- [templates/hazelcast5-multidc-ec2/ansible.cfg](./templates/hazelcast5-multidc-ec2/ansible.cfg)
- [templates/hazelcast5-multidc-ec2/async-tests.yaml](./templates/hazelcast5-multidc-ec2/async-tests.yaml)
- [templates/hazelcast5-multidc-ec2/client-hazelcast.xml](./templates/hazelcast5-multidc-ec2/client-hazelcast.xml)
- [templates/hazelcast5-multidc-ec2/hazelcast.xml](./templates/hazelcast5-multidc-ec2/hazelcast.xml)
- [templates/hazelcast5-multidc-ec2/setup](./templates/hazelcast5-multidc-ec2/setup)
- [templates/hazelcast5-multidc-ec2/teardown](./templates/hazelcast5-multidc-ec2/teardown)
- [templates/hazelcast5-multidc-ec2/tests.yaml](./templates/hazelcast5-multidc-ec2/tests.yaml)
- [templates/hazelcast5-multidc-ec2/.gitignore](./templates/hazelcast5-multidc-ec2/.gitignore)

### What the new template adds

The new template introduces:
- a `dcs:`-based inventory plan schema
- single-region multi-DC provisioning
- bounded two-region provisioning
- private data-plane communication between DCs using VPC peering when two regions are involved
- public operator access for SSH and Management Center
- per-DC AMI overrides for nodes, load generators, and MC
- a separate EC2 key pair in the secondary region so cross-region launches work correctly

### Main Terraform Structure

[templates/hazelcast5-multidc-ec2/aws/main.tf](./templates/hazelcast5-multidc-ec2/aws/main.tf) is the core of the implementation.

It adds:
- region/DC grouping derived from `inventory_plan.yaml`
- one public subnet per DC
- one route table per region
- one shared VPC and IGW per region, supplied by the user
- one VPC peering link between the primary and secondary region when two regions are used
- separate role security groups for:
  - nodes
  - load generators
  - Management Center
- role-based EC2 instances split into:
  - primary region resources
  - secondary region resources
- flat Terraform outputs:
  - `nodes`
  - `loadgenerators`
  - `mc`

Those flat outputs are what allow the existing simulator inventory model to keep working.

## Tests Added

Added test file:
- [src/simulator/tests/test_inventory_terraform.py](./src/simulator/tests/test_inventory_terraform.py)

What it covers:
- legacy nested Terraform output shape
- flat Terraform output shape
- map/object Terraform output shape

This test exists specifically to protect the compatibility layer added in `inventory_terraform.py`.

## Example and Runbook Material Added

A new examples area was added:

- [examples/multi-dc](./examples/multi-dc)

Added files:
- [examples/multi-dc/README_TEST.md](./examples/multi-dc/README_TEST.md)
- [examples/multi-dc/smoke-tests.yaml](./examples/multi-dc/smoke-tests.yaml)
- [examples/multi-dc/regression-single-dc-3nodes.inventory_plan.yaml](./examples/multi-dc/regression-single-dc-3nodes.inventory_plan.yaml)
- [examples/multi-dc/managed-single-region-2az-3nodes.inventory_plan.yaml](./examples/multi-dc/managed-single-region-2az-3nodes.inventory_plan.yaml)
- [examples/multi-dc/managed-two-region-3nodes.inventory_plan.yaml](./examples/multi-dc/managed-two-region-3nodes.inventory_plan.yaml)
- [examples/multi-dc/existing-cluster-three-region.inventory.yaml](./examples/multi-dc/existing-cluster-three-region.inventory.yaml)
- [examples/multi-dc/existing-cluster-three-region.client-hazelcast.xml](./examples/multi-dc/existing-cluster-three-region.client-hazelcast.xml)

### What the runbook adds

[README_TEST.md](./examples/multi-dc/README_TEST.md) adds:
- Docker-first smoke-test instructions
- shared Maven repo usage through `./mvnrepo`
- `SIM_IMAGE` as the image selector
- regression single-DC scenario
- single-region multi-DC scenario
- two-region multi-DC scenario
- Management Center access commands
- report and log retrieval commands
- an appendix for:
  - discovering VPCs and Internet Gateways
  - discovering matching AMIs in another region
  - creating a non-overlapping custom VPC and IGW for a second region

## Files Added Only for Example Data

These were added as supporting example inputs, not as simulator runtime changes:
- [examples/multi-dc/existing-cluster-three-region.inventory.yaml](./examples/multi-dc/existing-cluster-three-region.inventory.yaml)
- [examples/multi-dc/existing-cluster-three-region.client-hazelcast.xml](./examples/multi-dc/existing-cluster-three-region.client-hazelcast.xml)

They document example topologies but do not change `existing-cluster` behavior.

## Notable Additions Versus Base Branch

In short, compared with `master`, the branch adds:
- a new managed multi-DC AWS template
- bounded cross-region support for that template
- per-DC AMI overrides
- importer compatibility for new Terraform output shapes
- a focused importer regression test
- a practical multi-DC smoke-test runbook and example plans

## Current Cleanup Note

The branch also contains two tracked bytecode artifacts that were added along with the importer test:
- [src/simulator/tests/__pycache__/test_inventory_terraform.cpython-311.pyc](./src/simulator/tests/__pycache__/test_inventory_terraform.cpython-311.pyc)
- [src/simulator/tests/__pycache__/test_inventory_terraform.cpython-314.pyc](./src/simulator/tests/__pycache__/test_inventory_terraform.cpython-314.pyc)

They are not part of the intended feature implementation and should ideally be removed before finalizing the branch.
