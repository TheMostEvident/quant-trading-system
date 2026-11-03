class FoundationError(Exception):
    """Base class for failures callers must not silently ignore."""


class ValidationError(FoundationError, ValueError):
    pass


class DataConflict(FoundationError):
    pass


class IntegrityFailure(FoundationError):
    pass


class EnvironmentFailure(FoundationError):
    pass


class WriterBusy(FoundationError):
    pass


class StorageFailure(FoundationError):
    pass
