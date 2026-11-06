# Recommended Phase 3 AWS integration

Phase 3 should adapt the verified local controller without changing its release
semantics:

1. Draft Terraform for one Amazon Linux 2023 EC2 instance, encrypted EBS,
   public subnet/route, security group, instance profile, ECR, SSM, CloudWatch,
   and narrowly scoped GitHub OIDC roles. Review `terraform plan`; do not apply
   until security and cost review is complete.
2. Bootstrap encrypted, versioned S3 state with native lockfile separately.
3. Replace local image tags with ECR digest references while retaining Git SHA
   and resolved digest in the same manifest model.
4. Deliver runtime settings through narrowly scoped SSM Parameter Store or
   Secrets Manager access without putting literal secrets in Terraform state,
   user data, workflow logs, or SSM command text.
5. Run the controller through SSM Run Command with the EC2 tag and document
   constrained IAM permissions. Keep the host-side lock in addition to GitHub
   workflow concurrency.
6. Configure Caddy HTTPS for an operator-controlled DNS name and restrict
   ingress to required web ports. Keep SSH, PostgreSQL, Docker, API slots, and
   the Caddy administration endpoint unexposed.
7. Send controller, API, worker, Caddy, and host metrics/logs to CloudWatch with
   bounded retention and alarms.
8. Repeat every Phase 2 scenario on Amazon Linux, then test a PostgreSQL backup
   restore into a disposable destination. Do not infer restore success merely
   from backup creation.

ECS, EKS, ALB, RDS, and CodeDeploy remain outside this project architecture.

## Phase 3B plan-review gates

- `artifact_bucket_name` is the one explicitly approved, account-owned release
  bucket; deploy documents accept only
  `s3://<that-bucket>/<artifact_release_prefix>/<40-char-sha>/shipsafe-release.tar.gz`.
  Terraform creates the encrypted, versioned, public-blocked bucket and limits
  both publisher and host IAM permissions to that prefix.
- The repository does not yet contain an executable GitHub publisher workflow.
  That workflow is a remaining Phase 3B prerequisite: it must assume the
  branch-scoped role, publish the archive and checksum to the approved prefix,
  push the image to the single ECR repository, resolve its digest, and invoke
  the constrained SSM document. Do not treat the bucket alone as publication.
- During feature review, set `deploy_branch = "feature/shipsafe"`. The resulting
  OIDC subject is branch-specific. A reviewed plan for eventual production must
  deliberately change it to `main` (or a protected GitHub Environment); one
  role never trusts both branches implicitly.
- Docker is stopped immediately after package installation and cannot start
  until `/data/shipsafe` is mounted and its canary names the expected EBS volume.
  If the attachment misses the first-boot wait, user data fails and Docker stays
  stopped. The `nofail` mount keeps the host reachable for SSM repair while the
  Docker systemd guard prevents PostgreSQL from falling back to the root disk.
