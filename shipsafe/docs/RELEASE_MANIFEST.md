# Release manifest and state specification

Local runtime files live under ignored `shipsafe/runtime/`. AWS controller records
live on persistent EBS at `/data/shipsafe/runtime`, with installed source under
`/opt/shipsafe/releases/<sha>`. These are operational records, not source-controlled
configuration. EBS state persistence does not preserve every host dependency.

## Release manifest

Each `releases/<release-id>.json` contains:

| Field | Meaning |
|---|---|
| `manifest_version` | Manifest format version, currently `1` |
| `release_id` | First 12 Git SHA characters plus first 12 image-ID characters |
| `git_sha` | Supplied source Git SHA |
| `image_id` | Docker content identity in full `sha256:` form |
| `image_ref` | Local release tag; always revalidated against `image_id` |
| `schema_mode` | `legacy` during Phase 2 |
| `legacy_revision` | Required database revision `d4e5f6a7b8c9` |
| `candidate_verified` | Candidate readiness/authentication completed |
| `worker_verified` | Synthetic worker probe completed |
| `status` | Registered, deploying, active, verified previous, or failed |
| timestamps | UTC registration, verification, activation, or failure times |
| `failure_reason` | Sanitized, single-line, bounded reason; never command output |

The local tag is not trusted by itself. Deploy and rollback inspect it and
require its actual image ID to equal the manifest before starting a slot.

## Active state

`state.json` records:

- active and previous release IDs;
- active slot and the release assigned to each slot;
- worker release ID;
- current deployment operation, phase, candidate, and previous identities;
- last synthetic worker event ID;
- the last 100 deployment-history outcomes.

Neither file contains API keys, database passwords, raw environment dumps, or
HTTP authorization headers. `compose.env` contains only image references and
the runtime-directory path. The operator-owned runtime environment file remains
separate and ignored.

## Compatibility gate

Phase 2 manifests declare legacy revision `d4e5f6a7b8c9`. Candidate readiness
and worker preflight are the live compatibility checks. Manual rollback accepts
only a release previously verified for both API and worker and declaring the
same legacy revision. Database downgrade is never attempted.

## Phase 3C publisher manifest

`deployment.json` in the deterministic archive records `manifest_version`, the
full committed Git SHA, ECR image digest reference, `schema_revision`,
`layout=repository-root`, and SHA256 of each included file. The archive excludes
credentials, Terraform, tests and runtime state. SSM verifies this identity and
file set before installing a SHA-named release directory. The controller's
host-local manifest additionally records `source_image_ref` (the original ECR
digest); `image_id` is Docker's config/content ID and differs from the ECR
manifest digest. Local tags remain revalidated against that content ID.

Production uses `compose.production.yaml`; local acceptance continues using
`compose.yaml`. See [AWS handover](AWS_HANDOVER.md).
