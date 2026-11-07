# ShipSafe known limitations

The current [AWS limitations and future work](AWS_HANDOVER.md#limitations-and-future-work)
are maintained in the handover guide. [AWS results](AWS_RESULTS.md) separates
live observations from local/offline tests and lists missing evidence.

Earlier Phase 2 limitations described loopback-only Caddy and untested AWS
deployment. Those phase boundaries are historical: AWS HTTPS deployment and
the gated automatic rollback exercise have now been demonstrated. Single-host
failure domains, worker replacement pauses, privileged Docker access, retained
rollback-image requirements and the fixed schema boundary still apply.
