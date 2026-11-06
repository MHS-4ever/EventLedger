#!/usr/bin/env bash
# EC2 user data — ShipSafe Amazon Linux 2023 bootstrap
#
# PURPOSE: Install system packages, enable SSM and CloudWatch agents,
#          prepare Docker, and mount the EBS data volume.
#
# SECRETS POLICY:
#   NO secrets appear in this script.
#   Runtime credentials (passwords, API keys) are fetched at deploy time
#   from SSM Parameter Store by the ShipSafe controller.
#   This script is stored in Terraform state and readable via IMDS — it must
#   never contain literal credential values.
#
# Template variables (substituted by Terraform templatefile()):
#   ${aws_region}     — AWS region
#   ${ecr_registry}   — ECR registry URL
#   ${cw_log_group}   — CloudWatch log group name
#   ${project_prefix} — project prefix for naming

set -euo pipefail
rm -f /var/lib/shipsafe-bootstrap.ready

LOG=/var/log/shipsafe-userdata.log
exec >> "$LOG" 2>&1
echo "=== ShipSafe userdata started: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------

dnf update -y
dnf install -y \
  docker \
  amazon-cloudwatch-agent \
  amazon-ssm-agent \
  python3 \
  python3.12 \
  python3-pip \
  git \
  jq

# Package installation may auto-start Docker. Keep both activation paths
# disabled until the dedicated EBS volume is mounted and verified.
systemctl disable --now docker.service docker.socket || true

# ENV_VAR interpolation requires SSM Agent >= 3.3.2746.0. Fail closed on old agents.
SSM_VERSION=$(amazon-ssm-agent -version | awk '{print $NF}')
if [ "$(printf '%s\n' 3.3.2746.0 "$SSM_VERSION" | sort -V | head -n1)" != "3.3.2746.0" ]; then
  echo "FATAL: SSM Agent >= 3.3.2746.0 required" >&2
  exit 1
fi
systemctl enable --now amazon-ssm-agent

# ---------------------------------------------------------------------------
# Docker Compose plugin
# ---------------------------------------------------------------------------

COMPOSE_VERSION="2.32.4"
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/v$${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ---------------------------------------------------------------------------
# Docker daemon configuration
# ---------------------------------------------------------------------------

mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'DAEMON'
{
  "log-driver": "awslogs",
  "log-opts": {
    "awslogs-region": "${aws_region}",
    "awslogs-group": "${cw_log_group}"
  },
  "live-restore": true,
  "userland-proxy": false
}
DAEMON

# Allow the ec2-user to run Docker without sudo.
usermod -aG docker ec2-user

# ---------------------------------------------------------------------------
# EBS data volume — discover exact target volume, format only if blank, mount by UUID
#
# STABLE VOLUME IDENTIFICATION:
#   Identifies the volume strictly by its provisioned AWS EBS Volume ID:
#   ${ebs_volume_id} (e.g. vol-0123456789abcdef0).
#   On Nitro instances (AL2023), the EBS volume serial is the volume ID without
#   hyphens: vol0123456789abcdef0.
#
# VERIFIED BLANK FORMATTING & DATA LOSS PREVENTION:
#   NEVER formats any device that contains an existing or unknown filesystem,
#   partition table, LVM/RAID metadata, or existing signatures.
#   Formats ONLY a completely blank block device (zero signatures).
#   If an existing ext4 filesystem is detected, data is strictly preserved.
#   If an unknown or foreign filesystem is detected, fails closed immediately.
#
# FAIL-CLOSED MOUNT GUARD:
#   Creates /data/shipsafe/.shipsafe_ebs_mounted canary file.
#   Application services and controller fail closed if this canary is absent,
#   preventing PostgreSQL from ever running or writing data to the root disk.
# ---------------------------------------------------------------------------

TARGET_VOL_ID="${ebs_volume_id}"
TARGET_VOL_SERIAL="$${TARGET_VOL_ID//-/}" # Remove hyphens for Nitro NVMe serial matching
DATA_MOUNT="/data/shipsafe"
mkdir -p "$DATA_MOUNT"

echo "Locating targeted EBS volume: $TARGET_VOL_ID (serial: $TARGET_VOL_SERIAL)..."
DATA_DEVICE=""
ROOT_DEV=$(findmnt -n -o SOURCE / | sed -E 's/(p[0-9]+|[0-9]+)$//')

for i in $(seq 1 30); do
  # Check udev symlink for the specific volume ID
  BY_ID_PATH="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$TARGET_VOL_SERIAL"
  if [ -b "$BY_ID_PATH" ]; then
    DATA_DEVICE=$(readlink -f "$BY_ID_PATH")
    break
  fi

  # Fallback: scan NVMe block devices and match udev serial
  for dev in /dev/nvme*n1; do
    if [ -b "$dev" ] && [ "$dev" != "$ROOT_DEV" ]; then
      DEV_SERIAL=$(udevadm info --query=property --name="$dev" 2>/dev/null | grep -E '^ID_SERIAL_SHORT=' | cut -d= -f2 || true)
      if [ "$DEV_SERIAL" = "$TARGET_VOL_SERIAL" ]; then
        DATA_DEVICE="$dev"
        break 2
      fi
    fi
  done
  sleep 2
done

if [ -z "$DATA_DEVICE" ] || [ ! -b "$DATA_DEVICE" ]; then
  echo "FATAL: Targeted EBS volume $TARGET_VOL_ID was not attached after 60s. Refusing to continue." >&2
  exit 1
fi

echo "Successfully verified target EBS block device: $DATA_DEVICE for $TARGET_VOL_ID"

# Inspect device for ANY filesystem, partition table, or data signatures
EXISTING_FS=$(blkid -s TYPE -o value "$DATA_DEVICE" || true)
EXISTING_SIGS=$(wipefs -n "$DATA_DEVICE" 2>/dev/null || true)
PARTITION_TABLE=$(lsblk --nodeps -no PTTYPE "$DATA_DEVICE" 2>/dev/null || true)

if [ "$EXISTING_FS" = "ext4" ]; then
  echo "Existing ext4 filesystem verified on $DATA_DEVICE (UUID=$(blkid -s UUID -o value "$DATA_DEVICE")). Preserving existing data."
elif [ -n "$EXISTING_FS" ] || [ -n "$EXISTING_SIGS" ] || [ -n "$PARTITION_TABLE" ]; then
  echo "FATAL: Device $DATA_DEVICE contains an unknown/non-ext4 filesystem ('$EXISTING_FS') or partition table ('$PARTITION_TABLE')!" >&2
  echo "Detected signatures:" >&2
  echo "$EXISTING_SIGS" >&2
  echo "Refusing to format or overwrite. Operator manual intervention required to prevent data loss." >&2
  exit 1
else
  echo "Device $DATA_DEVICE is completely blank (0 signatures detected). Initializing ext4 filesystem..."
  mkfs.ext4 -L shipsafe-data -m 1 "$DATA_DEVICE"
fi

# Determine persistent UUID for safe /etc/fstab entry
FS_UUID=$(blkid -s UUID -o value "$DATA_DEVICE")
if [ -z "$FS_UUID" ]; then
  echo "FATAL: Failed to read UUID from $DATA_DEVICE after verification." >&2
  exit 1
fi

# Append to /etc/fstab if not present
if ! grep -q "$FS_UUID" /etc/fstab; then
  echo "UUID=$FS_UUID $DATA_MOUNT ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
fi

# Mount and strictly verify mount point
mount "$DATA_MOUNT" || mount -a

if ! mountpoint -q "$DATA_MOUNT"; then
  echo "FATAL: Mount verification failed for $DATA_MOUNT! Refusing to continue." >&2
  exit 1
fi

echo "Persistent EBS volume successfully mounted at $DATA_MOUNT (UUID=$FS_UUID)"

# Create persistent mount canary file to prevent services from ever writing to root filesystem
CANARY_FILE="$DATA_MOUNT/.shipsafe_ebs_mounted"
if [ ! -f "$CANARY_FILE" ]; then
  {
    echo "EBS_VOLUME_ID=$TARGET_VOL_ID"
    echo "EBS_VOLUME_UUID=$FS_UUID"
    echo "INITIALIZED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$CANARY_FILE"
  chmod 0400 "$CANARY_FILE"
fi

# Refuse every Docker start (including socket activation after reboot) unless
# systemd can mount the dedicated data filesystem and its canary identifies the
# exact provisioned EBS volume. This prevents PostgreSQL restart policies from
# silently creating /data/shipsafe/postgres on the root filesystem.
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/shipsafe-data.conf <<DOCKER_GUARD
[Unit]
RequiresMountsFor=$DATA_MOUNT
After=data-shipsafe.mount

[Service]
ExecStartPre=/usr/bin/mountpoint -q $DATA_MOUNT
ExecStartPre=/usr/bin/grep -Fxq EBS_VOLUME_ID=$TARGET_VOL_ID $CANARY_FILE
DOCKER_GUARD

systemctl daemon-reload
systemctl enable docker.service
systemctl start docker.service

# ---------------------------------------------------------------------------
# Directory structure on persistent EBS storage
# ---------------------------------------------------------------------------

# PostgreSQL 18 mounts this parent at /var/lib/postgresql and creates 18/docker.
# Fix only the parent metadata; never traverse or reset existing cluster contents.
if ! mountpoint -q "$DATA_MOUNT" || ! grep -Fxq "EBS_VOLUME_ID=$TARGET_VOL_ID" "$CANARY_FILE"; then
  echo "FATAL: refusing PostgreSQL directory setup without the expected EBS mount/canary" >&2
  exit 1
fi
PG_PARENT="$DATA_MOUNT/postgres"
if [ -L "$PG_PARENT" ] || { [ -e "$PG_PARENT" ] && [ ! -d "$PG_PARENT" ]; }; then
  echo "FATAL: PostgreSQL parent must be a real directory, not a symlink or file" >&2
  exit 1
fi
install -d -o 999 -g 999 -m 0700 -- "$PG_PARENT"

# ShipSafe controller runtime state & release manifests
mkdir -p "$DATA_MOUNT/runtime"
chmod 0700 "$DATA_MOUNT/runtime"

# Caddy persistent certificates, ACME account keys, and runtime state
# Preserves Let's Encrypt certificates across container restarts and slot switches
mkdir -p "$DATA_MOUNT/caddy/data" "$DATA_MOUNT/caddy/config"
chmod 0755 "$DATA_MOUNT/caddy"
chmod 0700 "$DATA_MOUNT/caddy/data" "$DATA_MOUNT/caddy/config"

# Database backups directory
mkdir -p "$DATA_MOUNT/backups"
chmod 0700 "$DATA_MOUNT/backups"

# ---------------------------------------------------------------------------
# Secure runtime parameter retrieval helper
# Fetches secrets from SSM Parameter Store into /data/shipsafe/runtime.env
# without echoing values into shell logs, process arguments, or bash history.
# ---------------------------------------------------------------------------

cat > /usr/local/bin/shipsafe-fetch-secrets <<'SECRETS_SCRIPT'
${fetch_secrets_script}
SECRETS_SCRIPT
chmod +x /usr/local/bin/shipsafe-fetch-secrets

# ---------------------------------------------------------------------------
# CloudWatch agent configuration
# ---------------------------------------------------------------------------

cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<CWCONFIG
{
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/var/log/shipsafe-userdata.log",
            "log_group_name": "${cw_log_group}",
            "log_stream_name": "{instance_id}/userdata"
          },
          {
            "file_path": "/var/log/messages",
            "log_group_name": "${cw_log_group}",
            "log_stream_name": "{instance_id}/messages"
          }
        ]
      }
    }
  },
  "metrics": {
    "namespace": "ShipSafe",
    "metrics_collected": {
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      },
      "disk": {
        "measurement": ["used_percent"],
        "resources": ["/", "/data/shipsafe"],
        "metrics_collection_interval": 60
      }
    }
  }
}
CWCONFIG

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config \
  -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
  -s

systemctl enable amazon-cloudwatch-agent

# ---------------------------------------------------------------------------
# ECR login helper
# ---------------------------------------------------------------------------

cat > /usr/local/bin/ecr-login <<ECRLOGIN
#!/usr/bin/env bash
set -euo pipefail
aws ecr get-login-password --region ${aws_region} | \
  docker login --username AWS --password-stdin ${ecr_registry}
ECRLOGIN
chmod +x /usr/local/bin/ecr-login

# ---------------------------------------------------------------------------
# ShipSafe controller installation
# ---------------------------------------------------------------------------

# The controller and compose.yaml are delivered by GitHub Actions at deploy time
# via SSM.  This placeholder records the expected installation path.
mkdir -p /opt/shipsafe

echo "=== ShipSafe userdata complete: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

mkdir -p /etc/shipsafe
printf '%s' '${deployment_config_base64}' | base64 -d > /etc/shipsafe/deployment.json
chmod 0644 /etc/shipsafe/deployment.json

# SSM documents reject deployments until every bootstrap step succeeds.
touch /var/lib/shipsafe-bootstrap.ready
