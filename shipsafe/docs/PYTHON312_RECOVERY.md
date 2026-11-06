# Recover the AL2023 controller Python interpreter

This procedure is for a separately approved live recovery after this hotfix is
reviewed/merged. No commands in this document were executed by the hotfix task.
Do not replace EC2, format EBS, reset controller state, republish artifacts or
change the system Python symlink to solve this import failure.

## Fix and Terraform impact

AL2023's `/usr/bin/python3` remains Python 3.9 for the OS. The controller requires
newer Python (`dataclass(slots=True)` requires 3.10+); production now explicitly
uses `/usr/bin/python3.12`. Future user data installs `python3.12` alongside
`python3`. AWS documents this side-by-side model:
https://docs.aws.amazon.com/linux/al2023/ug/python.html

The common SSM guard fails clearly if that exact executable is absent, before
secrets retrieval, extraction, marker writes or controller operations. Archive
validation and the secret helper remain on their existing compatible interpreter.
No symlink/alternatives change or application/container refactor is involved.

After merge, generate a NEW authenticated plan with normal S3 locking from main.
Expected changes, assuming no unrelated drift:

- In-place content/version updates to `aws_ssm_document.deploy`,
  `aws_ssm_document.status` and `aws_ssm_document.rollback`.
- No EC2 update/replacement: `ignore_changes = [ami, user_data]` remains intact.
- No EBS, KMS, SSM parameter, ECR image or S3 archive changes.

This is an expected diff from the configuration, not a new live-plan result.
Stop if the real plan includes unrelated changes, additions, replacements or
removals. Apply only after separate review/approval. Do not reuse an old saved
full-provisioning/recovery plan, taint/replace the instance, remove ignore_changes,
rerun cloud-init/user data, or broaden GitHub IAM to arbitrary shell commands.

Once approved document updates have been applied, inspect each document's
**default version** to confirm it contains the explicit interpreter and guard.
SendCommand without a version uses the default; do not assume a new latest
version is the selected default. Installing a package and updating documents do
not themselves run application bootstrap.

## Identify and preserve the failed attempt

1. From the main Terraform workspace, read the actual `ec2_instance_id` and
   `ebs_data_volume_id` outputs. Verify account `542468167362`, Region `us-east-1`,
   instance tags and attached volume. Use the explicit instance ID in SSM;
   do not run against a broad tag selection.
2. Confirm no ShipSafe workflow/SSM command is still running. Inspect the failed
   command's parameters: Operation, GitSha, ImageRef, ArtifactS3Uri and
   ArtifactSha256. Retain the exact original digest/URI/SHA/checksum; none are
   invented here. Do not display runtime.env or decrypted parameter values.
3. Preserve `/data/shipsafe/pending-release-root`, `current-release-root` if
   present, `/data/shipsafe/runtime/state.json` if present, release manifests,
   `/opt/shipsafe/releases/<original-sha>`, its `.artifact-sha256` and
   `deployment.json`. Do not delete/reset/re-extract them blindly. The confirmed
   failure occurred at controller import, so state might not have been created;
   inspect rather than assume.

## One-time Python installation via Systems Manager

Use an authorized **operator/admin** identity and Run Command's
`AWS-RunShellScript` document for the single verified instance. The GitHub deploy
role intentionally cannot run that arbitrary-shell document; do not expand it.
Preview/approve this payload before sending it. SSM runs it as root:

```bash
exec /bin/bash <<'SHIPSAFE_PY312_INSTALL'
set -euo pipefail
system_python_before=$(readlink -f /usr/bin/python3)
system_version_before=$(/usr/bin/python3 -c 'import sys; print(sys.version.split()[0])')
if [ ! -x /usr/bin/python3.12 ]; then
  dnf install -y python3.12
fi
/usr/bin/python3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 required"; print(sys.version.split()[0])'
test "$(readlink -f /usr/bin/python3)" = "$system_python_before"
test "$(/usr/bin/python3 -c 'import sys; print(sys.version.split()[0])')" = "$system_version_before"
echo 'Python 3.12 verified; system python3 target/version unchanged.'
SHIPSAFE_PY312_INSTALL
```

This installs only the versioned package and necessary dependencies. It does not
run a general package update, change symlinks, restart Docker, reboot EC2, modify
SSM parameters or run any controller action. If package availability/version or
system-Python checks fail, stop and inspect the command result; do not alter the
system interpreter or retry bootstrap blindly.

## Inspect, then retry the original release separately

After installation and approved SSM document updates:

1. Verify `/usr/bin/python3.12 --version`, exact EBS mount/canary and bootstrap
   completion marker. Inspect the current/pending release-root files read-only;
   each root must match `/opt/shipsafe/releases/<40 lowercase hex characters>`.
   Check the corresponding directory, manifest and `.artifact-sha256` against
   the original failed command's release identifiers/checksum.
2. On that verified directory, `/usr/bin/python3.12 shipsafe/controller.py --help`
   checks controller import/argument parsing without executing bootstrap. Do not
   omit `--help` or substitute `bootstrap` during this check.
3. If state.json exists, inspect only its release/operation/phase metadata. If it
   indicates a healthy active deployment, an in-progress operation or a failure
   different from this confirmed import error, stop for a state-specific recovery
   review. Do not delete state to force a bootstrap.
4. With explicit retry approval, invoke the updated `shipsafe-deploy` document
   using the **original** Operation=bootstrap and original GitSha, ECR digest,
   S3 URI and archive SHA256 from the failed command. This avoids rebuilding or
   overwriting the already verified image/archive. Do not dispatch the publisher
   merely to fix the host interpreter: publishing the hotfix commit selects a new
   source SHA and can create a different release.
5. The existing template verifies the archive again and reuses the SHA directory
   only if `.artifact-sha256` matches. Leave the pending marker for the successful
   document to promote/clear normally. Verify SSM success, controller state, API
   health/authentication, independent worker and trusted public HTTPS afterward.

The one-time install, Terraform document apply and bootstrap retry are distinct
live mutations requiring approval. None are performed by creating this PR.
