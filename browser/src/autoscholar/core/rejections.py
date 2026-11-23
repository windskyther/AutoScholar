"""Only for checks proven to run before an external request is dispatched."""


class LocalRequestRejected(ValueError):
    """A local admission failure, never a timeout or an unknown remote result."""

    dispatched = False
