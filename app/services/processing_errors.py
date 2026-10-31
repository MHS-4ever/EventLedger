class ProcessingFailure(Exception):
    """Base class for controlled, sanitized processing failures."""


class RetryableFailure(ProcessingFailure):
    """A controlled failure that may consume another processing attempt."""


class PermanentFailure(ProcessingFailure):
    """A controlled failure that must terminally fail the event."""


class LeaseOwnershipLost(Exception):
    """The claim is no longer current or its processing lease has expired."""


class ProcessingStateError(Exception):
    """Committed processing state violates an internal worker invariant."""
