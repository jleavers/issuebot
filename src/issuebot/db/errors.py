"""Errors raised by the database package. Messages never contain the database URL."""


class DatabaseError(Exception):
    """Base class; ``message`` has passed through ``redact``."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StoreUnavailableError(DatabaseError):
    """A connection-level failure: reconnect and retry the write."""


class StoreError(DatabaseError):
    """A statement failed for a reason a retry would not fix: drop the item."""


class MigrationError(DatabaseError):
    """Migrations could not be discovered or applied."""
