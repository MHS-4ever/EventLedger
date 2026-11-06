exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
[[ -v SSM_ReleaseId ]] || { echo 'SSM Agent >= 3.3.2746.0 required' >&2; exit 1; }
[[ "$SSM_ReleaseId" =~ ^([0-9a-f]{12}-[0-9a-f]{12})?$ ]] || exit 1
${guard}
args=()
if [[ -n "$SSM_ReleaseId" ]]; then args=(--release-id "$SSM_ReleaseId"); fi
cd /opt/shipsafe
python3 shipsafe/controller.py --root /opt/shipsafe --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime rollback "$${args[@]}"
SHIPSAFE_SSM_BASH
