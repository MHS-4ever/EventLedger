# Project 02 — ShipSafe: Automated Cloud Deployment & Rollback

**Builder portfolio project | Planned: 5–7 November 2026 (stretch target; stabilization may extend beyond this)**  
**Status: Planned; implementation not started.**  
**Application: EventLedger (Project 01), existing Python/FastAPI + PostgreSQL + worker.**

## Mission

Build and demonstrate a secure AWS release pipeline for EventLedger. A code push must run automated checks, build an immutable Docker artifact, authenticate to AWS without static access keys, deploy into an inactive application slot, verify health and functionality, and either promote the candidate or leave/restore the last known-good release. Deployment and rollback must have auditable evidence.

This is a **single-host educational blue/green deployment**, not high availability. The design intentionally avoids an ALB, NAT gateway, Kubernetes, and paid managed database for the initial version.

## Definitions of done

- `terraform fmt`, `validate`, `plan`, and controlled `apply` provision the lab infrastructure reproducibly.
- GitHub Actions uses AWS OIDC with repository/branch or environment-scoped trust; no long-lived AWS keys.
- PR/push CI runs the existing EventLedger tests, verifies the container can start, and performs appropriate security checks.
- An EventLedger image is built once and published to private Amazon ECR with immutable digest and Git commit SHA metadata.
- Deployment uses AWS Systems Manager Run Command or equivalent narrowly scoped AWS-native remote execution, not inbound SSH.
- Candidate runs without touching the active traffic path and passes liveness, readiness (including a DB check), and safe API smoke tests.
- Traffic is switched through a reverse proxy without stopping the previous healthy API container.
- Automated pre-promotion failure never affects the live version. Automated post-promotion failure switches traffic back, marks release failed, and preserves diagnostics.
- EventLedger worker deployment is explicitly controlled and checked with a safe test event. Its independent rollout/rollback limitations are documented.
- Immutable release records include digest/SHA, previous version, UTC deployment time, outcome, check results, and rollback reason.
- CloudWatch receives useful logs/metrics and a deliberately induced failure can be diagnosed.
- Database remains persistent across application upgrades; one verified database backup/restore procedure exists.
- Successful, failed, and manually reversed releases are demonstrated in recorded terminal/log evidence and a README architecture/runbook.
- AWS resource usage is cost-monitored, and teardown/backups are documented.

## Recommended architecture

```
GitHub (EventLedger source)
  -> GitHub Actions CI: tests, lint/security checks, Docker build/test
  -> AWS OIDC -> short-lived IAM role
  -> Amazon ECR (image digest, Git SHA)
  -> AWS SSM Run Command (restricted to tagged EC2 instance)
  -> EC2 Amazon Linux 2023 + Docker Compose
        Caddy (HTTPS reverse proxy)
             |-- API blue  <- active release
             |-- API green <- candidate release
        EventLedger worker (separately rolled)
        PostgreSQL (single persistent private Docker volume / EBS)
        CloudWatch log/metric agent

S3 (versioned + encrypted Terraform state, native lockfile)
Terraform -> VPC/subnet/security group/EC2/profile/ECR/SSM/IAM/logs
```

- One EC2 instance, usually `t3.medium` for comfortable Docker + database + worker + two API containers. Actual sizing and regional pricing must be confirmed before provisioning. A smaller instance may work after measuring RAM.
- One public subnet / route to an Internet Gateway for a low-cost learning setup. Do not provision a NAT Gateway by default. Restrict ingress to 443 (and 80 only if needed for TLS certificate challenge/redirect); never expose PostgreSQL or Docker ports. No inbound SSH/22. Use IMDSv2 and EBS encryption.
- EC2 instance profile can pull only the necessary ECR repository, read narrowly scoped runtime settings/secrets, and be managed via SSM. GitHub deploy role can only push to the intended ECR repo and dispatch commands to the tagged EC2 instance; Terraform provisioning permissions are separate.
- Use Caddy as a lightweight HTTPS reverse proxy (with a user-controlled DNS name). Build routing config so `blue` or `green` can be selected with config validation + graceful reload. No assumption of automatic rollback on application failure: our deployment controller implements that.
- PostgreSQL runs on the host for this **lab**, with persistent storage shared by both application versions. The database is a single point of failure. Consider RDS later if evolving the project toward production use.
- Secrets are never committed, inserted into Terraform state as literal passwords, put in EC2 user data, or echoed into build/deploy logs. Runtime credentials are sourced via tightly scoped AWS secrets/parameter access; document the risks of exposing credentials to container environments and process logs.
- Use an S3 state bucket with versioning, encryption, blocked public access and `use_lockfile=true`. Bootstrap that bucket separately from the main infrastructure.

## App readiness and database compatibility

Existing `GET /health` may be only liveness. Verify behavior before reusing. Add a readiness endpoint (for example `/ready`) that performs a bounded DB connection/check and returns non-2xx on dependency failure; avoid exposing credentials or exception traces. Docker `HEALTHCHECK` helps detect container health, but Docker status alone does not verify the API's actual behavior.

A candidate must pass an authenticated, **safe, non-destructive** API smoke test in addition to /ready. Do not submit genuine business fulfillment requests as health checks. The smoke test may use a dedicated synthetic event/test account or read-only contract check.

**Schema migration rule:** migrations are separate release steps. For this first project, permit **backward-compatible (expand-only)** migrations so old and new APIs work on the same DB. Destructive changes or changed semantics must block automatic promotion/rollback and require a documented forward-fix or DB restore plan. An application image rollback does not reverse PostgreSQL data or schema changes.

**Worker rule:** keep the old worker active during candidate API verification, then perform a controlled worker upgrade after API promotion. Drain/stop old worker, start candidate worker, verify a safe event and lease behavior. If worker verification fails, restore old worker, then (if needed) switch API back; confirm retry/idempotency rules protect processing during forced stops. State openly that exact zero-downtime worker rollback is not promised.

## Release algorithm

1. CI tests source and confirms the container build works. Fail closed.
2. Acquire a deployment concurrency lock so two promotions cannot race.
3. Build/push the release image once; record exact ECR digest and Git SHA, never use mutable `latest` as the rollback identity.
4. SSM starts the image in the inactive blue/green API slot and waits, with bounded retries/timeouts, for healthy startup/readiness.
5. Perform safe candidate API tests directly on the internal candidate slot. If tests fail, retain active traffic, stop candidate, mark failed, retain logs.
6. Preserve active release identity; validate new Caddy configuration and gracefully switch routing to the candidate.
7. Run HTTPS external smoke test and short observation window. If failed, restore old proxy route (old API remains alive), verify old endpoint, and report rollback.
8. Roll worker separately as described above, with explicit verification and revert on failure.
9. Only after successful verification mark new release active and record metadata. Retain at least one known-good older digest, manifest, and container during the verification window.
10. Support manual `rollback` to a previously verified digest; block rollback to an incompatible database schema. Ensure deploy and rollback are idempotent, re-entrant where possible, and safe after interrupted execution.

## CI/CD security controls

- Least-privilege `permissions`: `contents: read` for most jobs; `id-token: write` only in jobs that assume AWS role. Restrict GitHub OIDC trust `aud=sts.amazonaws.com`, `sub` to intended repository/branch or protected environment.
- PRs from forks have no AWS deployment credentials; deploy only from allowed branches and, if applicable, a GitHub Environment gate. Check environment protection availability for repository visibility/plan.
- Serialize deployment using GitHub concurrency **and** a host-side lock, since manual SSM or retries can still overlap.
- Separate infrastructure administrator role from image publisher/deployer role and EC2 runtime role.
- Pin GitHub Actions to maintained versions or commit SHAs; verify provenance of third-party actions. Use dependency scanning where it is practical.
- Set network timeout/readiness thresholds, rollback timeout, and clear error handling. Never print API keys or raw secret environment dumps.

## Execution plan

### Day 1 — AWS foundation and local release contract

**Implementation**
- Inspect existing EventLedger startup commands, Dockerfile, migrations, worker boot, health endpoint, and integration-test dependencies.
- Document blue/green traffic/worker/database contract. Create release, readiness, and smoke-check scripts locally.
- Configure AWS CLI access using a non-root administrative identity; verify account and region, credits, and billing alerts.
- Bootstrap an encrypted/versioned S3 Terraform state bucket with native lockfile.
- Write Terraform for VPC/subnet, public routing, security group, EC2, EBS, instance role/profile, ECR, CloudWatch log groups, and narrowly scoped IAM/OIDC configuration.
- Apply, verify SSM instance managed, check outbound access and ECR permissions; no inbound SSH.

**Acceptance:** A reproducible plan/apply produces a reachable-by-SSM host, secure network rules, and an ECR registry. Capture Terraform/SSM evidence without secrets.

### Day 2 — CI, image release and verified deployment

**Implementation**
- Add CI workflow running pytest, relevant lint/security checks, and Docker smoke build.
- Add GitHub OIDC trust and verify short-lived credential exchange.
- Add release workflow (push main or dispatch according to branch policy). Push image by Git SHA and use immutable digest.
- Install/bootstrap Docker Compose, Caddy, log collection, and runtime config on the EC2 host.
- Deploy existing EventLedger API + PostgreSQL + worker. Ensure persistent DB and prove a sample event is processed.
- Add inactive slot candidate, bounded readiness and authenticated safe smoke testing, release manifest and promotion routine.

**Acceptance:** One green end-to-end CI/CD run deploys a healthy immutable image, and users can access the stable HTTPS endpoint.

### Day 3 — Rollback, fault injection, observability, proof

**Implementation**
- Build proxy promotion + automatic reversal with release locking and records.
- Add worker controlled deployment and reversal, including lease/retry verification.
- Inject deliberate failures: startup crash, database-readiness failure (without damaging production data), post-switch API fault, and stopped worker.
- Confirm successful recovery: old digest still serves requests; failed release identified by SHA with cause; worker eventually resumes safely.
- Collect CloudWatch app/worker logs, EC2 metrics, and a basic alarm; set retention limits.
- Back up PostgreSQL and test restore to a separate disposable location. Document limitations.
- Record demo screenshots/logs, architecture, cost estimate, security design and teardown/runbook.

**Acceptance:** One happy deployment, one rejected pre-promotion candidate, one post-switch automatic rollback, and one operator-triggered rollback are proven with evidence. If the worker and DB recovery tests need longer, extend beyond three days rather than claiming completion.

## Test matrix

| Scenario | Expected behavior | Proof to retain |
|---|---|---|
| Healthy update | CI passes; candidate promoted; previous digest tracked | GitHub run + active SHA/version |
| Unit/integration test fails | No deploy or image promotion | Failed CI + current live version |
| New API fails startup | Candidate rejected; old API serves traffic | SSM logs + stable endpoint response |
| New API starts but cannot reach DB | Readiness fails; no promotion | Readiness response + DB check logs |
| New API breaks safe business/API contract | Smoke test fails; old remains | HTTP test outcome + version |
| Fault after promotion | Route switches back; old API stays available | Release log + proxy config + HTTPS check |
| Worker killed during event handling | Lease expires and recoverability/idempotency proven | Attempt history + final event state |
| Worker update fails | Old worker restored, no unbounded retries | Logs + event processing proof |
| Migration incompatible with old API | Release blocked / requires operator decision | Compatibility guard output |
| Two deployments triggered close together | No simultaneous traffic switches | Workflow concurrency and host lock logs |
| Manual rollback to known good | Operator can choose verified older digest | Recorded command + final SHA |
| Database backup recovery | Safe restore works on disposable destination | Restore verification (not only backup file) |

## Cost and region safeguards

- AWS credits are **not** proof that a service is free. Confirm current credit eligibility, account plan, per-region price, and whether new credits apply to selected services.
- `t3.medium` is 4 GiB RAM and 2 vCPU; US East baseline on-demand example is about $0.0418/hour, **not a quote for the chosen region**. Running 24/7 costs much more than 3 days of testing. See https://aws.amazon.com/ec2/instance-types/t3/
- In-use and idle public IPv4 addresses have a $0.005/hour charge. https://aws.amazon.com/vpc/pricing/
- EBS, ECR storage, CloudWatch ingestion/alarms, S3 state, optional Secrets Manager, and public network transfer may incur charges. Avoid NAT gateways and ALB for this exercise. https://aws.amazon.com/ecr/pricing/ and https://aws.amazon.com/cloudwatch/pricing/
- Set an AWS Budget notification (alert, not guaranteed hard spending cap) and tag every resource `Project=ShipSafe` and `Owner=Builder`. Verify running resources at the end of each day.
- After the demo: export non-sensitive evidence, preserve a verified DB backup if required, run controlled `terraform destroy` for disposable infrastructure, inspect residual EBS snapshots/ECR images/Elastic IPs, and retain state bucket only if needed (it can cost money too).

## Suggested repository ownership

For a *working release pipeline*, prefer integrating deployment workflows and app-level tests into **EventLedger's application repository**, so a push triggers deployment without cross-repository tokens. Make **ShipSafe a distinct portfolio case study** documenting the infrastructure, architecture, rollout/rollback scripts, demos and lessons. A separate ShipSafe infrastructure repository is possible, but cross-repo source checkout or workflow triggering must be planned explicitly rather than introducing an unnecessary personal access token. Decide final layout by verifying the current EventLedger repo visibility and automation rights.

Suggested logical layout (adjust to repo topology):

```
.github/workflows/ci.yml
.github/workflows/deploy.yml
infra/shipsafe/bootstrap/
infra/shipsafe/terraform/
deploy/compose.yml
deploy/Caddyfile
scripts/deploy.sh
scripts/rollback.sh
scripts/smoke_test.sh
scripts/release_status.sh
docs/shipsafe/architecture.md
docs/shipsafe/runbook.md
docs/shipsafe/test-evidence.md
docs/shipsafe/cost-and-teardown.md
```

## Scope exclusions / honest limitations

- This is **not** multi-AZ/high-availability architecture; EC2 failure can take down API, DB and worker.
- It does **not** promise rollback of destructive DB migrations or data mutations.
- It does **not** require ECS/EKS, ALB, RDS, auto-scaling or zero-downtime worker processing for the initial portfolio demonstration.
- CloudWatch alone will not automatically restore older code; the application deployment logic must implement rollback.
- Do not describe a release as production-grade until sustained load/security/cost/recovery testing supports the claim.

## Starting point for Day 1

1. Confirm EventLedger's actual Dockerfiles, worker entrypoint, readiness/health implementation, secrets configuration, migrations, database persistence and tests.
2. Check AWS account credit/billing dashboard, pick Region based on service availability/cost/latency, and configure a modest budget alert.
3. Draft and validate Terraform/IAM architecture **before** any create/apply.
4. Provision only after a reviewed `terraform plan` and security/cost checklist.

## Current reference documentation (checked 4 November 2026)

- GitHub OIDC/AWS: https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws
- AWS OIDC trust limitations: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html
- GitHub Actions environment protection: https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments
- GitHub concurrency: https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency
- AWS Systems Manager Run Command: https://docs.aws.amazon.com/systems-manager/latest/userguide/run-command.html
- Terraform S3 backend and lockfile: https://developer.hashicorp.com/terraform/language/backend/s3
- Amazon ECR push: https://docs.aws.amazon.com/AmazonECR/latest/userguide/docker-push-ecr-image.html
- Docker Compose service readiness: https://docs.docker.com/reference/compose-file/services/
- Caddy graceful config reload: https://caddyserver.com/docs/getting-started
- AWS EC2 IAM instance roles: https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_use_switch-role-ec2.html
- AWS CloudWatch agent: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Install-CloudWatch-Agent.html
