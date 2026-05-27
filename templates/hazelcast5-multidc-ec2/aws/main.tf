locals {
  settings    = yamldecode(file("../inventory_plan.yaml"))
  private_key = file("../${local.settings.keypair.private_key}")
  public_key  = file("../${local.settings.keypair.public_key}")
  dcs         = local.settings.dcs
  dcs_by_name = { for dc in local.dcs : dc.name => dc }
  dc_cidrs    = [for dc in local.dcs : dc.cidr_block]
  dc_regions  = toset([for dc in local.dcs : dc.region])
  dc_vpcs     = toset([for dc in local.dcs : dc.vpc_id])
  dc_igws     = toset([for dc in local.dcs : dc.internet_gateway_id])
  common_resource_tags = merge({
    team  = local.settings.team
    type  = local.settings.type
    Owner = local.settings.owner
  }, try(local.settings.extraTags, {}))

  node_instances = flatten([
    for dc in local.dcs : [
      for index in range(try(dc.nodes.count, 0)) : {
        key = "${dc.name}-node-${index}"
        dc  = dc
      }
    ]
  ])

  loadgenerator_instances = flatten([
    for dc in local.dcs : [
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
}

provider "aws" {
  profile = "default"
  region  = local.dcs[0].region
}

resource "aws_key_pair" "keypair" {
  key_name   = "simulator-keypair-${local.settings.basename}"
  public_key = local.public_key

  lifecycle {
    precondition {
      condition     = length(local.dc_regions) == 1
      error_message = "The initial hazelcast5-multidc-ec2 managed implementation supports a single AWS region only."
    }

    precondition {
      condition     = length(local.dc_vpcs) == 1
      error_message = "All DCs must use the same VPC in the initial hazelcast5-multidc-ec2 managed implementation."
    }

    precondition {
      condition     = length(local.dc_igws) == 1
      error_message = "All DCs must use the same Internet Gateway in the initial hazelcast5-multidc-ec2 managed implementation."
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

resource "aws_subnet" "dc_subnet" {
  for_each                = local.dcs_by_name
  vpc_id                  = each.value.vpc_id
  cidr_block              = each.value.cidr_block
  availability_zone       = each.value.availability_zone
  map_public_ip_on_launch = true

  tags = merge({
    Name = "Simulator Public Subnet ${local.settings.basename} ${each.key}"
  }, local.common_resource_tags)
}

resource "aws_route_table" "route_table" {
  vpc_id = local.dcs[0].vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = local.dcs[0].internet_gateway_id
  }

  tags = merge({
    Name = "Simulator Public Subnet Route Table ${local.settings.basename}"
  }, local.common_resource_tags)
}

resource "aws_route_table_association" "dc_route_table_association" {
  for_each       = local.dcs_by_name
  subnet_id      = aws_subnet.dc_subnet[each.key].id
  route_table_id = aws_route_table.route_table.id
}

resource "aws_security_group" "node_sg" {
  name        = "simulator-security-group-node-${local.settings.basename}"
  description = "Security group for simulator nodes"
  vpc_id      = local.dcs[0].vpc_id

  tags = merge({
    Name = "Simulator Node Security Group ${local.settings.basename}"
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

resource "aws_security_group" "loadgenerator_sg" {
  name        = "simulator-security-group-loadgenerator-${local.settings.basename}"
  description = "Security group for simulator load generators"
  vpc_id      = local.dcs[0].vpc_id

  tags = merge({
    Name = "Simulator Load Generator Security Group ${local.settings.basename}"
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

resource "aws_security_group" "mc_sg" {
  name        = "simulator-security-group-mc-${local.settings.basename}"
  description = "Security group for simulator Management Center"
  vpc_id      = local.dcs[0].vpc_id

  tags = merge({
    Name = "Simulator MC Security Group ${local.settings.basename}"
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

resource "aws_instance" "nodes" {
  for_each               = { for item in local.node_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = local.settings.nodes.ami
  instance_type          = local.settings.nodes.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.node_sg.id]
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

resource "aws_instance" "loadgenerators" {
  for_each               = { for item in local.loadgenerator_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = local.settings.loadgenerators.ami
  instance_type          = local.settings.loadgenerators.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.loadgenerator_sg.id]
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

resource "aws_instance" "mc" {
  for_each               = { for item in local.mc_instances : item.key => item }
  key_name               = aws_key_pair.keypair.key_name
  ami                    = local.settings.mc.ami
  instance_type          = local.settings.mc.instance_type
  availability_zone      = each.value.dc.availability_zone
  subnet_id              = aws_subnet.dc_subnet[each.value.dc.name].id
  vpc_security_group_ids = [aws_security_group.mc_sg.id]

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

output "nodes" {
  value = values(aws_instance.nodes)
}

output "loadgenerators" {
  value = values(aws_instance.loadgenerators)
}

output "mc" {
  value = values(aws_instance.mc)
}
