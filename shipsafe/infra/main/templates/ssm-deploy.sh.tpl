# aws:runShellScript can launch /bin/sh; explicitly select Bash.
exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
# No inline parameter fallback: older SSM Agents must fail closed.
: "$${SSM_ImageRef:?SSM Agent >= 3.3.2746.0 required}"
: "$${SSM_GitSha:?missing GitSha}"
: "$${SSM_ReadyTimeout:?missing ReadyTimeout}"
: "$${SSM_WorkerTimeout:?missing WorkerTimeout}"
: "$${SSM_ObservationSeconds:?missing ObservationSeconds}"
[[ -v SSM_ArtifactS3Uri && -v SSM_ArtifactSha256 ]] || exit 1
image_pattern='${image_pattern}'
[[ "$SSM_ImageRef" =~ $image_pattern ]] || exit 1
[[ "$SSM_GitSha" =~ ^[0-9a-f]{40}$ ]] || exit 1
[[ "$SSM_ReadyTimeout" =~ ^(6[0-9]|[7-9][0-9]|[12][0-9]{2}|300)$ ]] || exit 1
[[ "$SSM_WorkerTimeout" =~ ^([3-9][0-9]|1[0-7][0-9]|180)$ ]] || exit 1
[[ "$SSM_ObservationSeconds" =~ ^([5-9]|[1-5][0-9]|60)$ ]] || exit 1
# A variable preserves regex metacharacters without shell parsing them as syntax.
artifact_pattern='${artifact_pattern}'
[[ "$SSM_ArtifactS3Uri" =~ $artifact_pattern ]] || exit 1
[[ "$SSM_ArtifactSha256" =~ ^([0-9a-f]{64})?$ ]] || exit 1
if [[ -n "$SSM_ArtifactS3Uri" ]]; then
  [[ -n "$SSM_ArtifactSha256" ]] || { echo 'Artifact checksum required' >&2; exit 1; }
  [[ "$SSM_ArtifactS3Uri" == */"$SSM_GitSha"/shipsafe-release.tar.gz ]] || exit 1
else
  [[ -z "$SSM_ArtifactSha256" ]] || exit 1
fi
${guard}
RUNTIME_ENV=/data/shipsafe/runtime.env
/usr/local/bin/shipsafe-fetch-secrets
if [[ -n "$SSM_ArtifactS3Uri" ]]; then
  tmpdir=$(mktemp -d /opt/shipsafe-release.XXXXXX)
  trap 'rm -rf -- "$tmpdir"' EXIT
  aws s3 cp "$SSM_ArtifactS3Uri" "$tmpdir/release.tar.gz"
  printf '%s  %s\n' "$SSM_ArtifactSha256" "$tmpdir/release.tar.gz" | sha256sum -c -
  # Reject traversal, links and special files before root extracts trusted publisher content.
  python3 - "$tmpdir/release.tar.gz" <<'SHIPSAFE_ARCHIVE'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    for item in archive.getmembers():
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit("Unsafe release archive")
SHIPSAFE_ARCHIVE
  mkdir -p /opt/shipsafe
  tar --no-same-owner --no-same-permissions -xzf "$tmpdir/release.tar.gz" -C /opt/shipsafe
fi
aws ecr get-login-password --region ${aws_region} | docker login --username AWS --password-stdin ${ecr_registry}
docker pull "$SSM_ImageRef"
cd /opt/shipsafe
python3 shipsafe/controller.py --root /opt/shipsafe --runtime-env "$RUNTIME_ENV" --runtime-dir /data/shipsafe/runtime deploy --image "$SSM_ImageRef" --git-sha "$SSM_GitSha" --ready-timeout "$SSM_ReadyTimeout" --worker-timeout "$SSM_WorkerTimeout" --observation-seconds "$SSM_ObservationSeconds"
SHIPSAFE_SSM_BASH
