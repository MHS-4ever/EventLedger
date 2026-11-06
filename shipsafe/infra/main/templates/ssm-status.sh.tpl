exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
${guard}
cd /opt/shipsafe
python3 shipsafe/controller.py --root /opt/shipsafe --runtime-env /data/shipsafe/runtime.env --runtime-dir /data/shipsafe/runtime status
SHIPSAFE_SSM_BASH
