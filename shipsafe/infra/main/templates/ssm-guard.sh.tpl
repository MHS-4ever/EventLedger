mountpoint -q /data/shipsafe || { echo 'FATAL: data filesystem is not mounted' >&2; exit 1; }
grep -Fxq 'EBS_VOLUME_ID=${ebs_volume_id}' /data/shipsafe/.shipsafe_ebs_mounted || { echo 'FATAL: incorrect EBS canary' >&2; exit 1; }
test -x /usr/local/bin/shipsafe-fetch-secrets || { echo 'FATAL: cloud-init bootstrap incomplete' >&2; exit 1; }
test -f /var/lib/shipsafe-bootstrap.ready || { echo 'FATAL: cloud-init bootstrap incomplete' >&2; exit 1; }
