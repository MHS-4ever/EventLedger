exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
[[ -v SSM_ReleaseId ]] || { echo 'SSM Agent >= 3.3.2746.0 required' >&2; exit 1; }
[[ "$SSM_ReleaseId" =~ ^([0-9a-f]{12}-[0-9a-f]{12})?$ ]] || exit 1
${guard}
exec 9>/data/shipsafe/publisher.lock
flock -n 9 || exit 1
release_root=$(cat /data/shipsafe/current-release-root)
[[ "$release_root" =~ ^/opt/shipsafe/releases/[0-9a-f]{40}$ ]] || exit 1
args=()
if [[ -n "$SSM_ReleaseId" ]]; then args=(--release-id "$SSM_ReleaseId"); fi
cd "$release_root"
python3 shipsafe/controller.py --root "$release_root" --project-name shipsafe-production --production-config /etc/shipsafe/deployment.json --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime rollback "$${args[@]}"
SHIPSAFE_SSM_BASH
