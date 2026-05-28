# This plan is intentionally additive and bounded:
# - it only backs the hazelcast5-multidc-ec2 template
# - it keeps the simulator-facing inventory flat (nodes/loadgenerators/mc)
# - it supports up to two AWS regions
# - operator access is public (SSH/MC), while cluster traffic stays on private IPs
#
# The user-facing topology comes from inventory_plan.yaml as a list of DCs. Each
# DC contributes a subnet/AZ placement and per-role counts, but the simulator
# runtime still sees a normal flat inventory after terraform_import().
locals {
  # Read the inventory plan once and derive all region/DC groupings from it.
  # The file is split into a "primary" and optional "secondary" region because
  # this template uses a bounded number of provider aliases rather than trying
  # to create providers dynamically.
  settings    = yamldecode(file("../inventory_plan.yaml"))
  private_key = file("../${local.settings.keypair.private_key}")
  public_key  = file("../${local.settings.keypair.public_key}")
  dcs         = local.settings.dcs
  dcs_by_name = { for dc in local.dcs : dc.name => dc }
  dc_cidrs    = [for dc in local.dcs : dc.cidr_block]
  region_names = distinct([
    for dc in local.dcs : dc.region
  ])
  primary_region       = local.region_names[0]
  has_secondary_region = length(local.region_names) > 1
  secondary_region     = local.has_secondary_region ? local.region_names[1] : local.primary_region

  primary_dcs = [
    for dc in local.dcs : dc if dc.region == local.primary_region
  ]
  secondary_dcs = [
    for dc in local.dcs : dc if local.has_secondary_region && dc.region == local.secondary_region
  ]
  primary_dcs_by_name   = { for dc in local.primary_dcs : dc.name => dc }
  secondary_dcs_by_name = { for dc in local.secondary_dcs : dc.name => dc }

  primary_region_vpcs = distinct([
    for dc in local.primary_dcs : dc.vpc_id
  ])
  secondary_region_vpcs = distinct([
    for dc in local.secondary_dcs : dc.vpc_id
  ])
  primary_region_igws = distinct([
    for dc in local.primary_dcs : dc.internet_gateway_id
  ])
  secondary_region_igws = distinct([
    for dc in local.secondary_dcs : dc.internet_gateway_id
  ])
  primary_vpc_id   = local.primary_region_vpcs[0]
  secondary_vpc_id = local.has_secondary_region ? local.secondary_region_vpcs[0] : local.primary_vpc_id
  primary_igw_id   = local.primary_region_igws[0]
  secondary_igw_id = local.has_secondary_region ? local.secondary_region_igws[0] : local.primary_igw_id

  common_resource_tags = merge({
    team  = local.settings.team
    type  = local.settings.type
    Owner = local.settings.owner
  }, try(local.settings.extraTags, {}))

  # Expand the per-DC counts into concrete instance descriptors. These locals
  # are later turned into for_each maps for the actual EC2 instances.
  primary_node_instances = flatten([
    for dc in local.primary_dcs : [
      for index in range(try(dc.nodes.count, 0)) : {
        key = "${dc.name}-node-${index}"
        dc  = dc
      }
    ]
  ])
  secondary_node_instances = flatten([
    for dc in local.secondary_dcs : [
      for index in range(try(dc.nodes.count, 0)) : {
        key = "${dc.name}-node-${index}"
        dc  = dc
      }
    ]
  ])

  primary_loadgenerator_instances = flatten([
    for dc in local.primary_dcs : [
      for index in range(try(dc.loadgenerators.count, 0)) : {
        key = "${dc.name}-loadgenerator-${index}"
        dc  = dc
      }
    ]
  ])
  secondary_loadgenerator_instances = flatten([
    for dc in local.secondary_dcs : [
      for index in range(try(dc.loadgenerators.count, 0)) : {
        key = "${dc.name}-loadgenerator-${index}"
        dc  = dc
      }
    ]
  ])

  mc_instances = [
    for index in range(try(local.settings.mc.count, 0)) : {
      key = "mc-${index}"
      dc  = local.dcs_by_name[local.settings.mc_dc]
    }
  ]
  primary_mc_instances   = local.dcs_by_name[local.settings.mc_dc].region == local.primary_region ? local.mc_instances : []
  secondary_mc_instances = local.has_secondary_region && local.dcs_by_name[local.settings.mc_dc].region == local.secondary_region ? local.mc_instances : []
}

# The default provider targets the first region seen in dcs; a second aliased
# provider is used only when the plan spans a second region.
provider "aws" {
  profile = "default"
  region  = local.primary_region
}

provider "aws" {
  alias   = "secondary"
  profile = "default"
  region  = local.secondary_region
}

resource "aws_key_pair" "keypair" {
  key_name   = "simulator-keypair-${local.settings.basename}"
  public_key = local.public_key

  lifecycle {
    # Keep validation close to the plan so invalid topologies fail before any
    # infrastructure is created.
    precondition {
      condition     = length(local.region_names) > 0
      error_message = "At least one DC must be declared in dcs."
    }

    precondition {
      condition     = length(local.region_names) <= 2
      error_message = "The managed hazelcast5-multidc-ec2 template currently supports up to two AWS regions."
    }

    precondition {
      condition     = length(local.primary_region_vpcs) == 1
      error_message = "All DCs in the primary region must use the same VPC."
    }

    precondition {
      condition     = !local.has_secondary_region || length(local.secondary_region_vpcs) == 1
      error_message = "All DCs in the secondary region must use the same VPC."
    }

    precondition {
      condition     = length(local.primary_region_igws) == 1
      error_message = "All DCs in the primary region must use the same Internet Gateway."
    }

    precondition {
      condition     = !local.has_secondary_region || length(local.secondary_region_igws) == 1
      error_message = "All DCs in the secondary region must use the same Internet Gateway."
    }

    precondition {
      condition     = contains(keys(local.dcs_by_name), local.settings.primary_dc)
      error_message = "primary_dc must match one of the names declared in dcs."
    }

    precondition {
      condition     = contains(keys(local.dcs_by_name), local.settings.mc_dc)
      error_message = "mc_dc must match one of the names declared in dcs."
    }
  }
}

resource "aws_key_pair" "secondary_keypair" {
  provider   = aws.secondary
  count      = local.has_secondary_region ? 1 : 0
  key_name   = aws_key_pair.keypair.key_name
  public_key = local.public_key
}

# Networking layout:
# - one public subnet per DC/AZ
# - one route table per region
# - one VPC and one Internet Gateway per region
# The plan reuses user-supplied VPC/IGW ids instead of creating new VPCs.
resource "aws_subnet" "primary_dc_subnet" {
  for_each                = local.primary_dcs_by_name
  vpc_id                  = each.value.vpc_id
  cidr_block              = each.value.cidr_block
  availability_zone       = each.value.availability_zone
  map_public_ip_on_launch = true

  tags = merge({
    Name = "Simulator Public Subnet ${local.settings.basename} ${each.key}"
  }, local.common_resource_tags)
}

resource "aws_subnet" "secondary_dc_subnet" {
  provider                = aws.secondary
  for_each                = local.secondary_dcs_by_name
  vpc_id                  = each.value.vpc_id
  cidr_block              = each.value.cidr_block
  availability_zone       = each.value.availability_zone
  map_public_ip_on_launch = true

  tags = merge({
    Name = "Simulator Public Subnet ${local.settings.basename} ${each.key}"
  }, local.common_resource_tags)
}

resource "aws_route_table" "primary_route_table" {
  vpc_id = local.primary_vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = local.primary_igw_id
  }

  tags = merge({
    Name = "Simulator Public Subnet Route Table ${local.settings.basename} ${local.primary_region}"
  }, local.common_resource_tags)
}

resource "aws_route_table" "secondary_route_table" {
  provider = aws.secondary
  count    = local.has_secondary_region ? 1 : 0
  vpc_id   = local.secondary_vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = local.secondary_igw_id
  }

  tags = merge({
    Name = "Simulator Public Subnet Route Table ${local.settings.basename} ${local.secondary_region}"
  }, local.common_resource_tags)
}

resource "aws_route_table_association" "primary_dc_route_table_association" {
  for_each       = local.primary_dcs_by_name
  subnet_id      = aws_subnet.primary_dc_subnet[each.key].id
  route_table_id = aws_route_table.primary_route_table.id
}

resource "aws_route_table_association" "secondary_dc_route_table_association" {
  provider       = aws.secondary
  for_each       = local.secondary_dcs_by_name
  subnet_id      = aws_subnet.secondary_dc_subnet[each.key].id
  route_table_id = aws_route_table.secondary_route_table[0].id
}

# When a second region is present, connect the two regional VPCs with one
# peering link and add private routes for every remote DC CIDR. This is the
# private data plane used by Hazelcast members, load generators, and MC.
resource "aws_vpc_peering_connection" "inter_region" {
  count       = local.has_secondary_region ? 1 : 0
  vpc_id      = local.primary_vpc_id
  peer_vpc_id = local.secondary_vpc_id
  peer_region = local.secondary_region
  auto_accept = false

  tags = merge({
    Name = "Simulator VPC Peering ${local.settings.basename}"
  }, local.common_resource_tags)
}

resource "aws_vpc_peering_connection_accepter" "inter_region" {
  provider                  = aws.secondary
  count                     = local.has_secondary_region ? 1 : 0
  vpc_peering_connection_id = aws_vpc_peering_connection.inter_region[0].id
  auto_accept               = true

  tags = merge({
    Name = "Simulator VPC Peering Accepter ${local.settings.basename}"
  }, local.common_resource_tags)
}

resource "aws_route" "primary_to_secondary" {
  for_each                  = local.has_secondary_region ? local.secondary_dcs_by_name : {}
  route_table_id            = aws_route_table.primary_route_table.id
  destination_cidr_block    = each.value.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.inter_region[0].id
}

resource "aws_route" "secondary_to_primary" {
  provider                  = aws.secondary
  for_each                  = local.has_secondary_region ? local.primary_dcs_by_name : {}
  route_table_id            = aws_route_table.secondary_route_table[0].id
  destination_cidr_block    = each.value.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection_accepter.inter_region[0].id
}

# Security groups are split by role and duplicated per region. They expose:
# - public SSH / simulator control ports for operator workflow compatibility
# - private Hazelcast / iperf / ICMP traffic across the declared DC CIDRs
resource "aws_security_group" "primary_node_sg" {
  name        = "simulator-security-group-node-${local.settings.basename}-${replace(local.primary_region, "-", "_")}"
  description = "Security group for simulator nodes"
  vpc_id      = local.primary_vpc_id

  tags = merge({
    Name = "Simulator Node Security Group ${local.settings.basename} ${local.primary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Hazelcast"
    from_port   = 5701
    to_port     = 5801
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "Simulator"
    from_port   = 9000
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Hazelcast-tpc"
    from_port   = 11000
    to_port     = 12000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_udp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "udp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_tcp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "secondary_node_sg" {
  provider    = aws.secondary
  count       = local.has_secondary_region ? 1 : 0
  name        = "simulator-security-group-node-${local.settings.basename}-${replace(local.secondary_region, "-", "_")}"
  description = "Security group for simulator nodes"
  vpc_id      = local.secondary_vpc_id

  tags = merge({
    Name = "Simulator Node Security Group ${local.settings.basename} ${local.secondary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Hazelcast"
    from_port   = 5701
    to_port     = 5801
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "Simulator"
    from_port   = 9000
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Hazelcast-tpc"
    from_port   = 11000
    to_port     = 12000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_udp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "udp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_tcp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "primary_loadgenerator_sg" {
  name        = "simulator-security-group-loadgenerator-${local.settings.basename}-${replace(local.primary_region, "-", "_")}"
  description = "Security group for simulator load generators"
  vpc_id      = local.primary_vpc_id

  tags = merge({
    Name = "Simulator Load Generator Security Group ${local.settings.basename} ${local.primary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Simulator"
    from_port   = 9000
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "iperf3_udp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "udp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_tcp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "secondary_loadgenerator_sg" {
  provider    = aws.secondary
  count       = local.has_secondary_region ? 1 : 0
  name        = "simulator-security-group-loadgenerator-${local.settings.basename}-${replace(local.secondary_region, "-", "_")}"
  description = "Security group for simulator load generators"
  vpc_id      = local.secondary_vpc_id

  tags = merge({
    Name = "Simulator Load Generator Security Group ${local.settings.basename} ${local.secondary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Simulator"
    from_port   = 9000
    to_port     = 9001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "iperf3_udp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "udp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "iperf3_tcp"
    from_port   = 3000
    to_port     = 3000
    protocol    = "tcp"
    cidr_blocks = local.dc_cidrs
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "primary_mc_sg" {
  name        = "simulator-security-group-mc-${local.settings.basename}-${replace(local.primary_region, "-", "_")}"
  description = "Security group for simulator Management Center"
  vpc_id      = local.primary_vpc_id

  tags = merge({
    Name = "Simulator MC Security Group ${local.settings.basename} ${local.primary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Management Center"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Management Center TLS"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "secondary_mc_sg" {
  provider    = aws.secondary
  count       = local.has_secondary_region ? 1 : 0
  name        = "simulator-security-group-mc-${local.settings.basename}-${replace(local.secondary_region, "-", "_")}"
  description = "Security group for simulator Management Center"
  vpc_id      = local.secondary_vpc_id

  tags = merge({
    Name = "Simulator MC Security Group ${local.settings.basename} ${local.secondary_region}"
  }, local.common_resource_tags)

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Management Center"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "Management Center TLS"
    from_port   = 8443
    to_port     = 8443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "ICMP"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = local.dc_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Instance resources are also split by region to match the bounded provider
# model. Per-DC AMI overrides are optional; when absent, top-level role AMIs are
# used. Tags prefixed with passthrough: are copied into inventory.yaml later.
resource "aws_instance" "primary_nodes" {
  for_each               = { for item in local.primary_node_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = try(each.value.dc.nodes.ami, local.settings.nodes.ami)
  instance_type          = local.settings.nodes.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.primary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.primary_node_sg.id]
  tenancy                = local.settings.nodes.tenancy

  tags = merge({
    Name                                       = "Simulator Node ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.nodes.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)
}

resource "aws_instance" "secondary_nodes" {
  provider               = aws.secondary
  for_each               = { for item in local.secondary_node_instances : item.key => item }
  key_name               = aws_key_pair.secondary_keypair[0].key_name
  ami                    = try(each.value.dc.nodes.ami, local.settings.nodes.ami)
  instance_type          = local.settings.nodes.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.secondary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.secondary_node_sg[0].id]
  tenancy                = local.settings.nodes.tenancy

  tags = merge({
    Name                                       = "Simulator Node ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.nodes.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)
}

resource "aws_instance" "primary_loadgenerators" {
  for_each               = { for item in local.primary_loadgenerator_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = try(each.value.dc.loadgenerators.ami, local.settings.loadgenerators.ami)
  instance_type          = local.settings.loadgenerators.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.primary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.primary_loadgenerator_sg.id]
  tenancy                = local.settings.loadgenerators.tenancy

  tags = merge({
    Name                                       = "Simulator Load Generator ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.loadgenerators.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)
}

resource "aws_instance" "secondary_loadgenerators" {
  provider               = aws.secondary
  for_each               = { for item in local.secondary_loadgenerator_instances : item.key => item }
  key_name               = aws_key_pair.secondary_keypair[0].key_name
  ami                    = try(each.value.dc.loadgenerators.ami, local.settings.loadgenerators.ami)
  instance_type          = local.settings.loadgenerators.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.secondary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.secondary_loadgenerator_sg[0].id]
  tenancy                = local.settings.loadgenerators.tenancy

  tags = merge({
    Name                                       = "Simulator Load Generator ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.loadgenerators.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)
}

# MC is bootstrapped with a small remote-exec step so the simulator can expose a
# ready-to-use browser endpoint after provisioning. It still talks to the
# cluster over private networking because the member addresses use private_ip.
resource "aws_instance" "primary_mc" {
  for_each               = { for item in local.primary_mc_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = try(each.value.dc.mc.ami, local.settings.mc.ami)
  instance_type          = local.settings.mc.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.primary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.primary_mc_sg.id]

  tags = merge({
    Name                                       = "Simulator MC ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.mc.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)

  connection {
    type        = "ssh"
    user        = local.settings.mc.user
    private_key = local.private_key
    host        = self.public_ip
  }

  provisioner "remote-exec" {
    inline = [
      "wget -q https://repository.hazelcast.com/download/management-center/hazelcast-management-center-5.0.tar.gz",
      "tar -xzvf hazelcast-management-center-5.0.tar.gz",
      "while [ ! -f /var/lib/cloud/instance/boot-finished ]; do echo 'Waiting for cloud-init...'; sleep 1; done",
      "sudo apt-get -y update",
      "sudo apt-get -y install openjdk-11-jdk",
      "nohup hazelcast-management-center-5.0/bin/start.sh  > mc.out 2>&1 &",
      "sleep 2"
    ]
  }
}

resource "aws_instance" "secondary_mc" {
  provider               = aws.secondary
  for_each               = { for item in local.secondary_mc_instances : item.key => item }
  key_name               = aws_key_pair.secondary_keypair[0].key_name
  ami                    = try(each.value.dc.mc.ami, local.settings.mc.ami)
  instance_type          = local.settings.mc.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.secondary_dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.secondary_mc_sg[0].id]

  tags = merge({
    Name                                       = "Simulator MC ${local.settings.basename} ${each.value.dc.name}"
    "passthrough:ansible_ssh_private_key_file" = local.settings.keypair.private_key
    "passthrough:ansible_user"                 = local.settings.mc.user
    "passthrough:dc"                           = each.value.dc.name
    "passthrough:region"                       = each.value.dc.region
    "passthrough:availability_zone"            = each.value.dc.availability_zone
  }, local.common_resource_tags)

  connection {
    type        = "ssh"
    user        = local.settings.mc.user
    private_key = local.private_key
    host        = self.public_ip
  }

  provisioner "remote-exec" {
    inline = [
      "wget -q https://repository.hazelcast.com/download/management-center/hazelcast-management-center-5.0.tar.gz",
      "tar -xzvf hazelcast-management-center-5.0.tar.gz",
      "while [ ! -f /var/lib/cloud/instance/boot-finished ]; do echo 'Waiting for cloud-init...'; sleep 1; done",
      "sudo apt-get -y update",
      "sudo apt-get -y install openjdk-11-jdk",
      "nohup hazelcast-management-center-5.0/bin/start.sh  > mc.out 2>&1 &",
      "sleep 2"
    ]
  }
}

# Outputs deliberately stay flat even though provisioning is multi-DC. That
# keeps backward compatibility with the simulator inventory importer and the
# rest of the runtime, which already understands nodes/loadgenerators/mc.
output "nodes" {
  value = concat(
    values(aws_instance.primary_nodes),
    values(aws_instance.secondary_nodes),
  )
}

output "loadgenerators" {
  value = concat(
    values(aws_instance.primary_loadgenerators),
    values(aws_instance.secondary_loadgenerators),
  )
}

output "mc" {
  value = concat(
    values(aws_instance.primary_mc),
    values(aws_instance.secondary_mc),
  )
}
