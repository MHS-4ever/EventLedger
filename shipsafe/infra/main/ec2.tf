################################################################################
# EC2 — Single-host deployment target
#
# Instance type: t3.medium (2 vCPU, 4 GiB RAM) is the recommended minimum for
# running Docker Compose with two API containers, PostgreSQL, and a worker.
# Confirm actual RAM requirements before provisioning.
#
# IMDSv2 is enforced (hop limit 1 — containers cannot reach instance metadata).
#
# DATA-LOSS WARNING
#   The EBS data volume holds all PostgreSQL data.
#   - Replacing the EC2 instance detaches the EBS volume.  Terraform will
#     attempt to delete it unless `prevent_destroy = true` is set or the volume
#     is manually detached and preserved.
#   - Run `terraform destroy` only after taking an EBS snapshot AND a pg_dump
#     backup to a separate location and verifying the restore.
#   - `lifecycle { prevent_destroy = true }` is set on the data volume.
#     Remove it only deliberately.
################################################################################

# ---------------------------------------------------------------------------
# Latest Amazon Linux 2023 AMI (official AWS-managed)
# ---------------------------------------------------------------------------

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-x86_64"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ---------------------------------------------------------------------------
# EC2 instance
# ---------------------------------------------------------------------------

resource "aws_instance" "shipsafe" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public.id
  vpc_security_group_ids = [aws_security_group.shipsafe.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  # IMDSv2 — required; hop limit 1 so containers cannot reach IMDS.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # Root volume: OS only.  Keep small; data lives on the separate EBS volume.
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true
  }

  # EC2 user data: minimal bootstrap only.
  # SECRETS MUST NEVER appear here — user data is readable via IMDS and stored
  # in the Terraform state file (which is itself in S3).
  # Only install system packages and enable SSM/CloudWatch agents here.
  # Runtime credentials are delivered via SSM Parameter Store at deploy time.
  user_data = base64encode(templatefile("${path.module}/templates/userdata.sh.tpl", {
    aws_region     = var.aws_region
    ecr_registry   = local.ecr_registry_url
    cw_log_group   = aws_cloudwatch_log_group.shipsafe.name
    project_prefix = var.project_prefix
    ebs_volume_id  = aws_ebs_volume.data.id
  }))

  # Ensure the instance profile and log group exist before the instance boots.
  depends_on = [
    aws_iam_instance_profile.ec2,
    aws_cloudwatch_log_group.shipsafe,
  ]

  tags = { Name = "${var.project_prefix}-host" }

  lifecycle {
    # AMI ID changes on each AL2023 update.  Ignore to avoid accidental replacement.
    ignore_changes = [ami, user_data]
  }
}

# ---------------------------------------------------------------------------
# Separate EBS data volume for PostgreSQL
#
# Kept separate from the root volume so it can survive instance replacement.
#
# DATA-LOSS WARNING — see module header above.
# ---------------------------------------------------------------------------

resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"
  encrypted         = true

  # Tag with the project so it can be identified after instance replacement.
  tags = { Name = "${var.project_prefix}-data" }

  lifecycle {
    # Prevent accidental deletion of the data volume by `terraform destroy`.
    # Remove this guard only after taking a verified pg_dump backup + EBS snapshot.
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.shipsafe.id

  # Ensure the instance stops before detaching so PostgreSQL flushes buffers cleanly.
  stop_instance_before_detaching = true

  # Do not force-detach on destroy; require clean detachment.
  force_detach = false

  # On destroy, do not skip detachment.
  skip_destroy = false
}
