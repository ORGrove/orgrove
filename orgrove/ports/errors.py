class PortError(Exception):
    """Base class for errors an adapter raises across a port."""


class NotFound(PortError):
    """The item, PR or run does not exist."""


class LedgerError(PortError):
    """A reserve/settle that would let budget accounting be faked: an unknown run, a replay, a double settle."""
