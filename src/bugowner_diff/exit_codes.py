"""Process exit codes returned by the ``bugowner-diff`` command."""

from enum import IntEnum


class ExitCode(IntEnum):
    """Exit status returned to the shell.

    Values follow the sysexits/shell conventions so a caller can tell the
    failure modes apart without parsing stderr:

    - ``OK`` -- the diff completed.
    - ``ERROR`` -- any unclassified failure.
    - ``USAGE`` -- bad argv, or an input file whose contents are unusable. A file
      that could not be opened or read is ``OSError`` and ``ERROR`` instead; the
      split is stated where ``InputError`` is defined.
    - ``TIMEOUT`` -- a subprocess exceeded its deadline.
    - ``MISSING_BINARY`` -- a required binary is not on ``PATH``.
    - ``INTERRUPT`` -- SIGINT, i.e. ``KeyboardInterrupt`` (128 + 2).
    """

    OK = 0
    ERROR = 1
    USAGE = 64
    TIMEOUT = 124
    MISSING_BINARY = 127
    INTERRUPT = 130
