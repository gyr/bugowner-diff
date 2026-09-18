"""Turn argv into a run, and a failure into an exit code.

This module owns two decisions and deliberately owns nothing else: what the
command line looks like, and which exit code each failure leaves behind. The
run itself belongs to :func:`~bugowner_diff.commands.diff.run_diff`, and the
four repositories are constructed here only because this is the one place that
knows a real run is wanted rather than a test double.

**No argument is validated here.** ``--ref`` meets the only ref allowlist in the
project inside
:mod:`bugowner_diff.repositories.remote_archive_repository`, ``--project`` meets
its own in :mod:`bugowner_diff.repositories.obs_project_repository`, and the
contents of ``-i`` meet the package-name allowlist in
:mod:`bugowner_diff.repositories.package_list_repository` -- each before the
value becomes an argv slot or an API path. A second check here would be a second
copy of a rule that already has exactly one definition, and two copies is how
they drift. What this module owes those chokepoints is not a re-check but
wiring discipline: the value reaches them unedited.

There is no default for ``-i``, ``--project`` or ``--ref``. The project name and
the ref appear in the help text as examples, which is a different thing: an
example is read by a person, while a default silently diffs something nobody
asked for when an argument is forgotten.

The order of the ``except`` arms below is load-bearing:

- ``KeyboardInterrupt`` is a ``BaseException``, so ``except Exception`` cannot
  reach it and it needs an arm of its own.
- ``BrokenPipeError`` *is* an ``OSError``, so its arm must sit above the
  ``OSError`` arm or it would never run.
- ``MissingBinaryError``, ``NetworkTimeoutError`` and ``InputError`` sit above
  their own base class for the same reason.
- There is no ``RuntimeError`` arm and no ``ValueError`` arm, on purpose.
  ``RecursionError`` is a ``RuntimeError``, so such an arm would report a
  genuine stack-depth defect as an orderly failure; a non-UTF-8 input file is
  already ``InputError`` at its raise site.

Only the catch-all prints a traceback, and it prints one unconditionally rather
than under ``-d``. A run is roughly two minutes of sequential network
round-trips against sources that change underneath it, so "re-run it with
``-d``" is not an instruction that reliably reproduces anything. The traceback
has to come out of the run that actually failed.

Every other arm prints one line and no traceback: those failures are all
described completely by their message, and a stack trace beside them only
teaches the reader to ignore stack traces.
"""

import argparse
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from bugowner_diff.commands.diff import run_diff
from bugowner_diff.exceptions import (
    BugownerDiffError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)
from bugowner_diff.exit_codes import ExitCode
from bugowner_diff.repositories.obs_owner_repository import ObsOwnerRepositoryImpl
from bugowner_diff.repositories.obs_project_repository import ObsProjectRepositoryImpl
from bugowner_diff.repositories.package_list_repository import PackageListRepositoryImpl
from bugowner_diff.repositories.remote_archive_repository import RemoteArchiveRepositoryImpl


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ownership diff and report how it went.

    Args:
        argv: Arguments to parse, without the program name. ``None`` means read
            them from ``sys.argv``, which is what the console script does.

    Returns:
        An :class:`~bugowner_diff.exit_codes.ExitCode`, which is an ``IntEnum``
        and therefore already the integer the shell wants. The mapping is the
        ladder of ``except`` arms and nothing else -- no failure's own return
        code is ever passed through as an exit code, because a remote tool's
        idea of "2" is not this tool's.

    Raises:
        SystemExit: Propagated from the parser for every exit but the usage
            error, so ``--help`` still exits 0 rather than being renamed into a
            return value.
    """
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # Only argparse's usage error becomes a return value. The 0 it exits with
        # for `--help` is already right, and translating that here would turn a
        # successful `-h` into a failure.
        if exc.code == 2:
            return ExitCode.USAGE
        raise
    logging.basicConfig(level=args.log_level)

    try:
        run_diff(
            package_list=PackageListRepositoryImpl(),
            project_repository=ObsProjectRepositoryImpl(),
            owner_repository=ObsOwnerRepositoryImpl(),
            archive_repository=RemoteArchiveRepositoryImpl(),
            input_path=args.input,
            output_path=args.output,
            project=args.project,
            ref=args.ref,
        )
        # Flushed here, inside the try, so a closed pipe is still this function's
        # to handle. Left to interpreter shutdown it would raise after every arm
        # below has run, printing `Exception ignored in: <_io.TextIOWrapper ...>`
        # and exiting 120 -- a code outside ExitCode entirely.
        sys.stdout.flush()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return ExitCode.INTERRUPT
    except BrokenPipeError:
        print("Output pipe closed before the report was complete.", file=sys.stderr)
        # The reader is gone, but the interpreter will still flush stdout on the
        # way out and raise again. Giving fd 1 somewhere harmless to drain is the
        # documented way to keep that second failure from outliving this one.
        os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
        return ExitCode.ERROR
    except MissingBinaryError as exc:
        print(exc, file=sys.stderr)
        return ExitCode.MISSING_BINARY
    except NetworkTimeoutError as exc:
        print(exc, file=sys.stderr)
        return ExitCode.TIMEOUT
    except InputError as exc:
        print(exc, file=sys.stderr)
        return ExitCode.USAGE
    except BugownerDiffError as exc:
        print(exc, file=sys.stderr)
        return ExitCode.ERROR
    except OSError as exc:
        # errno already says more than a wrapper could, which is why the
        # repositories let these through raw for this arm to name.
        print(exc, file=sys.stderr)
        return ExitCode.ERROR
    except Exception:
        traceback.print_exc()
        print(
            "This is a bug in bugowner-diff. Please report it with the traceback above.",
            file=sys.stderr,
        )
        return ExitCode.ERROR
    return ExitCode.OK


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        A parser for the six options declared below, plus the ``-h`` argparse
        adds itself. ``-d`` and ``-q`` share one
        destination through a mutually exclusive group, so the log level is one
        value with one default rather than two flags a later reader has to
        combine. There is no ``-v``: this project makes exactly two logging
        calls and both are warnings, so an INFO level would promise output that
        does not exist.
    """
    parser = argparse.ArgumentParser(
        prog="bugowner-diff",
        description="Diff package bugownership between an OBS project and the SLFO "
        "maintainership document.",
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        required=True,
        help="File listing the packages to diff, one name per line.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="File to write the CSV report to. Omit it to write to stdout.",
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Project whose listing and owners the 15 column reports, e.g. SUSE:SLE-15-SP7:GA.",
    )
    parser.add_argument(
        "--ref",
        required=True,
        help="Git ref of the maintainership document the 16 column reports, e.g. slfo-main.",
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-d",
        "--debug",
        dest="log_level",
        action="store_const",
        const=logging.DEBUG,
        default=logging.WARNING,
        help="Log at DEBUG level. It sets the log level and nothing else.",
    )
    verbosity.add_argument(
        "-q",
        "--quiet",
        dest="log_level",
        action="store_const",
        const=logging.ERROR,
        help="Log at ERROR level, silencing the warnings about unusable source data.",
    )
    return parser
