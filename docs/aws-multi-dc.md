# AWS Multi-DC Scenarios

## Why

Multi-DC scenarios make it possible to test Hazelcast behavior across AWS
availability zones and regions rather than only on a single cluster. They cover
healthy operation, member loss, DC loss, and stretched-cluster behavior with
the same inventory and test-run workflow used by other managed EC2 projects.

## How to use it

Create an image-bundled scenario with `docker-sim tutorial-init`, then configure
the generated inventory plan with the AWS region, availability zones, VPC,
subnet, AMI, and licensing values that are valid for your account. Run the
normal lifecycle through `docker-sim`: apply inventory, install required
components, verify connectivity, run the selected test, inspect its report, and
destroy the inventory.

These scenarios can include Management Center and an observability host. They
are managed cloud environments: credentials and quota must be checked before
provisioning, and `inventory destroy` must be run after every successful or
failed experiment that created resources.

The [multi-DC tutorial](../examples/multi-dc/README_TEST.md) is the executable
guide. It contains the AWS sign-in and discovery commands, scenario selection,
configuration details, verification, and provider-side cleanup checks.
