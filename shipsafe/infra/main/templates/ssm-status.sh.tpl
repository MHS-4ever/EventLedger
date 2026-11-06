exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
${guard}
exec 9>/data/shipsafe/publisher.lock
flock -n 9 || exit 1
if [[ -f /data/shipsafe/current-release-root ]]; then
  release_root=$(cat /data/shipsafe/current-release-root)
else
  release_root=$(cat /data/shipsafe/pending-release-root)
fi
[[ "$release_root" =~ ^/opt/shipsafe/releases/[0-9a-f]{40}$ ]] || exit 1
cd "$release_root"
/usr/bin/python3.12 shipsafe/controller.py --root "$release_root" --project-name shipsafe-production --production-config /etc/shipsafe/deployment.json --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime status
SHIPSAFE_SSM_BASH
