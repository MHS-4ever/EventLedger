# Manual ShipSafe rollback demonstration

This is an opt-in production traffic test, available only through the manual
ShipSafe release workflow on this repository's `main` branch. Normal runs use
`failure_mode=none`. No host or application environment file enables injection.

The live demonstration and normal same-SHA retry are recorded in
[AWS results](../shipsafe/docs/AWS_RESULTS.md). For a future repeat, first confirm
the reviewed SSM document is the selected default version, then start with a
working active release and a new candidate SHA on `main`.
Keep `main` on that SHA for both workflow runs:

1. Record the active API release, slot and worker release with `operation=status`.
2. Dispatch `operation=deploy`, `failure_mode=post-promotion`. The controller
   verifies the candidate, switches traffic, deliberately stops the candidate API,
   observes failure and restores the previous API. The worker is never replaced.
   Expect the SSM command and workflow to fail, even when rollback succeeds.
3. Inspect restricted controller logs and run `operation=status` with
   `failure_mode=none`. Confirm the previous release and slot are active, the
   worker release is unchanged, and no deployment is pending. Verify public
   `/health`, `/ready` and the authenticated API smoke check. Do not retry if
   recovery is incomplete.
4. Dispatch a fresh workflow on the same SHA with `operation=deploy`,
   `failure_mode=none`. The publisher reuses the immutable image and deterministic
   archive; the controller retries the recorded failed release normally.

Do not use bootstrap, rollback or status with injection. An already active SHA
is an idempotent no-op, so it cannot demonstrate this failure. Use a fresh
candidate first. Selecting “rerun” on the failed workflow retains the injected
input; dispatch a new normal run instead.

The test deliberately causes a brief API outage between candidate shutdown and
proxy restoration. Rollback still depends on the previous API, proxy and host
remaining healthy. Offline tests demonstrate ordering and state restoration;
they do not replace the separate live state and external HTTPS observations.
Injection changes no health endpoint, IAM policy or schema. Ordinary controller verification on the subsequent
successful deployment retains its existing synthetic worker probe.
