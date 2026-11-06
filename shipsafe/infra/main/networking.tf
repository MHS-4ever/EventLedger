################################################################################
# VPC — Networking foundation
#
# Design: one public subnet per AZ (single AZ for cost), one Internet Gateway,
# one route table.  No NAT Gateway (cost: ~$0.045/hour/AZ).
# PostgreSQL, Docker, and Caddy admin ports are never exposed publicly.
################################################################################

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

resource "aws_vpc" "shipsafe" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project_prefix}-vpc" }
}

# ---------------------------------------------------------------------------
# Public subnet (single AZ — intentional low-cost lab design)
# ---------------------------------------------------------------------------

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.shipsafe.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = "${var.aws_region}${var.availability_zone_suffix}"
  map_public_ip_on_launch = true

  tags = { Name = "${var.project_prefix}-public" }
}

# ---------------------------------------------------------------------------
# Internet Gateway + route
# ---------------------------------------------------------------------------

resource "aws_internet_gateway" "shipsafe" {
  vpc_id = aws_vpc.shipsafe.id

  tags = { Name = "${var.project_prefix}-igw" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.shipsafe.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.shipsafe.id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.shipsafe.id
  }

  tags = { Name = "${var.project_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------------------
# Security group
#
# INBOUND
#   443/tcp  — HTTPS from anywhere (required for public access)
#    80/tcp  — HTTP from anywhere (required for ACME HTTP-01 certificate
#               challenge; Caddy will redirect to HTTPS)
#
# All other inbound traffic is denied — specifically:
#   22/tcp   SSH        — no inbound SSH; use SSM Session Manager if needed
#   5432/tcp PostgreSQL — backend only; never exposed publicly
#   2375/tcp Docker API — backend only; never exposed publicly
#   2019/tcp Caddy admin— container-internal only; never exposed publicly
#   8000/tcp API slots  — backend only; never exposed publicly
#
# OUTBOUND — unrestricted so the host can reach:
#   SSM endpoints     (443)
#   ECR               (443)
#   CloudWatch        (443)
#   OS package repos  (443/80)
#   Docker Hub        (443, for Caddy and postgres images)
# ---------------------------------------------------------------------------

resource "aws_security_group" "shipsafe" {
  name        = "${var.project_prefix}-sg"
  description = "ShipSafe EC2: allow HTTPS and HTTP inbound; deny SSH, Postgres, Docker."
  vpc_id      = aws_vpc.shipsafe.id

  tags = { Name = "${var.project_prefix}-sg" }
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "HTTPS — required for public access and Caddy HTTPS termination"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "https_v6" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "HTTPS IPv6"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv6         = "::/0"
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "HTTP — required for ACME HTTP-01 TLS certificate challenge"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_ingress_rule" "http_v6" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "HTTP IPv6 for ACME challenge"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv6         = "::/0"
}

resource "aws_vpc_security_group_egress_rule" "all_outbound" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "Allow all outbound (SSM, ECR, CloudWatch, package repos, Docker Hub)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all_outbound_v6" {
  security_group_id = aws_security_group.shipsafe.id
  description       = "Allow all outbound IPv6"
  ip_protocol       = "-1"
  cidr_ipv6         = "::/0"
}
