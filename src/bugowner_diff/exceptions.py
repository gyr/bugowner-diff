"""Exception taxonomy raised by bugowner-diff."""


class BugownerDiffError(Exception):
    """Base class for every error this package raises deliberately.

    The CLI catches this type to map a failure onto an
    :class:`~bugowner_diff.exit_codes.ExitCode`. The tree is flat on purpose:
    every concrete type below is a direct child, so one ``except`` arm can
    never swallow a sibling meant for a different exit code.

    ``OSError`` escapes this tree by design -- ``errno`` already says more
    about a failed open, read or rename than a wrapper could add, so the
    repositories let it through raw and the CLI maps it. The one other
    deliberate escape is the documented ``ValueError`` of
    :class:`~bugowner_diff.repositories.package_list_repository.PackageListRepository`,
    which predates :class:`InputError` and is mapped the same way. Anything
    else that escapes is a bug and is reported as one, with its traceback.
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


class InputError(BugownerDiffError):
    """A value supplied by the user is unusable.

    Raised for a malformed command-line argument or an unusable input file --
    the failures the user can fix by re-running with different arguments. The
    CLI maps it onto :attr:`~bugowner_diff.exit_codes.ExitCode.USAGE`.

    Message-only, unlike the two types above: what makes an input unusable
    differs per call site, and a fixed constructor argument would force every
    one of them to invent a label that says less than the message does.
    """


class DataSourceError(BugownerDiffError):
    """A remote data source answered, but not with anything usable.

    Raised when ``osc`` or ``git`` exits non-zero, or when the body it printed
    is not the document it was supposed to be -- malformed XML, a forbidden
    DOCTYPE, an unexpected root element, or a body over its size cap. Nothing
    the user can fix by changing arguments, so the CLI maps it onto the generic
    :attr:`~bugowner_diff.exit_codes.ExitCode.ERROR`.

    A deliberate replacement for ``RuntimeError`` at these call sites:
    ``RecursionError`` is a ``RuntimeError`` subclass, so a handler for the
    latter also catches a crash in the parser and reports it as a bad remote
    document.
    """
