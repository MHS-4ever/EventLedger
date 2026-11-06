# aws:runShellScript can launch /bin/sh; explicitly select Bash.
exec /bin/bash <<'SHIPSAFE_SSM_BASH'
set -euo pipefail
[[ -v SSM_Operation && "$SSM_Operation" =~ ^(bootstrap|deploy)$ ]] || exit 1
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
[[ -n "$SSM_ArtifactS3Uri" && -n "$SSM_ArtifactSha256" ]] || exit 1
${guard}
# Serialize publication, extraction and controller operations on the host.
exec 9>/data/shipsafe/publisher.lock
flock -n 9 || { echo 'Another release operation is running' >&2; exit 1; }
docker info >/dev/null
RUNTIME_ENV=/data/shipsafe/runtime.env
/usr/local/bin/shipsafe-fetch-secrets
tmpdir=$(mktemp -d /opt/shipsafe-release.XXXXXX)
trap 'rm -rf -- "$tmpdir"' EXIT
aws s3 cp "$SSM_ArtifactS3Uri" "$tmpdir/release.tar.gz"
printf '%s  %s\n' "$SSM_ArtifactSha256" "$tmpdir/release.tar.gz" | sha256sum -c -
python3 - "$tmpdir/release.tar.gz" "$tmpdir/root" "$SSM_GitSha" "$SSM_ImageRef" <<'SHIPSAFE_ARCHIVE'
import hashlib, json, pathlib, sys, tarfile
archive_path, destination, sha, image = sys.argv[1:]
with tarfile.open(archive_path) as archive:
    seen = set()
    for item in archive.getmembers():
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != item.name or not item.isfile() or item.name in seen:
            raise SystemExit("Unsafe release archive")
        seen.add(item.name)
    manifest = json.load(archive.extractfile("deployment.json"))
    if manifest["manifest_version"] != 1 or manifest["layout"] != "repository-root":
        raise SystemExit("Unsupported release manifest")
    if manifest["git_sha"] != sha or manifest["image_ref"] != image or manifest["schema_revision"] != "d4e5f6a7b8c9":
        raise SystemExit("Release identity mismatch")
    if seen != set(manifest["files"]) | {"deployment.json"}:
        raise SystemExit("Release file set mismatch")
    for name, digest in manifest["files"].items():
        if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != digest:
            raise SystemExit("Release file checksum mismatch")
    pathlib.Path(destination).mkdir()
    archive.extractall(destination)
SHIPSAFE_ARCHIVE
release_root="/opt/shipsafe/releases/$SSM_GitSha"
mkdir -p /opt/shipsafe/releases
if [[ -e "$release_root" ]]; then
  [[ "$(cat "$release_root/.artifact-sha256")" == "$SSM_ArtifactSha256" ]] || exit 1
else
  printf '%s' "$SSM_ArtifactSha256" > "$tmpdir/root/.artifact-sha256"
  mv "$tmpdir/root" "$release_root"
fi
aws ecr get-login-password --region ${aws_region} | docker login --username AWS --password-stdin ${ecr_registry}
docker pull "$SSM_ImageRef"
printf '%s' "$release_root" > /data/shipsafe/pending-release-root
cd "$release_root"
args=("$SSM_Operation" --image "$SSM_ImageRef" --git-sha "$SSM_GitSha" --ready-timeout "$SSM_ReadyTimeout" --worker-timeout "$SSM_WorkerTimeout")
if [[ "$SSM_Operation" == deploy ]]; then args+=(--observation-seconds "$SSM_ObservationSeconds"); fi
/usr/bin/python3.12 shipsafe/controller.py --root "$release_root" --project-name shipsafe-production --production-config /etc/shipsafe/deployment.json --runtime-env "$RUNTIME_ENV" --runtime-dir /data/shipsafe/runtime "$${args[@]}"
printf '%s' "$release_root" > /data/shipsafe/.current-release-root.tmp
mv /data/shipsafe/.current-release-root.tmp /data/shipsafe/current-release-root
rm -f /data/shipsafe/pending-release-root
SHIPSAFE_SSM_BASH
