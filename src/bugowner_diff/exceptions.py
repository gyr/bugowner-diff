"""Exception taxonomy raised by bugowner-diff."""


class BugownerDiffError(Exception):
    """Base class for every error this package raises deliberately.

    The CLI catches this type to map a failure onto an
    :class:`~bugowner_diff.exit_codes.ExitCode`; anything escaping it is a bug.
    """


class MissingBinaryError(BugownerDiffError):
    """A binary required to reach a data source is not on ``PATH``."""

    def __init__(self, binary: str) -> None:
        """Record the binary that could not be found.

        Args:
            binary: Name of the missing executable, e.g. ``"osc"`` or ``"git"``.
        """
        self.binary = binary
        super().__init__(f"Required binary not found: {binary}")


class NetworkTimeoutError(BugownerDiffError):
    """A remote call exceeded its deadline."""

    def __init__(self, label: str, timeout: float) -> None:
        """Record which call timed out and after how long.

        Args:
            label: Human-readable name of the call, e.g. ``"osc api"``.
            timeout: Deadline in seconds that was exceeded.
        """
        self.label = label
        self.timeout = timeout
        # ``:g`` trims the trailing zero so 30.0 reads as "30". It flips to
        # scientific notation above 1e6, which no call site can reach: every
        # timeout is an internal constant of a few dozen seconds, never user input.
        super().__init__(f"{label} timed out after {timeout:g}s")
