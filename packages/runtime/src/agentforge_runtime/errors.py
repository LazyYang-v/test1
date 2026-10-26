"""Runtime ownership errors."""

from __future__ import annotations


class LeaseLostError(RuntimeError):
    """The attempt no longer owns the Task and must not write lifecycle state."""

    code = "LEASE_LOST"


class TaskNotRunningError(RuntimeError):
    """The Task left the running state (terminal, cancelling, or deadline)."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)
