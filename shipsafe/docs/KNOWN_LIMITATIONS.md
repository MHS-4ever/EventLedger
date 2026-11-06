# Phase 2 known limitations

- One host, one PostgreSQL container, one volume, and one proxy are all single
  points of failure. Blue/green protects application replacement, not host or
  database availability.
- Caddy uses loopback HTTP for Phase 2. Public DNS, HTTPS issuance, and secure
  inbound rules belong to AWS integration.
- Worker replacement has a deliberate processing pause. Zero-downtime worker
  rollout is not promised.
- The synthetic worker check writes a clearly marked durable EventLedger event
  and fulfillment. It should use a dedicated source identity.
- Worker compatibility is checked at worker startup. A later contract change
  still requires controlled worker replacement.
- The schema-contract table does not exist yet. Phase 2 remains pinned to
  `d4e5f6a7b8c9`; no migration is run during deploy or rollback.
- Release state is host-local. Loss of the host can lose deployment history
  even if the database volume is backed up.
- A privileged Docker operator can inspect container environment variables.
  AWS SSM/Secrets Manager reduces distribution risk but does not remove this
  Docker-level exposure.
- Local tags can be deliberately changed by a privileged operator. Image-ID
  revalidation detects drift before deployment but is not cryptographic image
  signing.
- Windows Docker Desktop differs from the target Linux host in bind mounts,
  filesystem locking, process signals, and path handling. Final acceptance must
  be repeated on Amazon Linux or a representative Linux VM.
- No claim is made for AWS deployment, external rollback, backup restoration,
  load behavior, high availability, or production readiness.
