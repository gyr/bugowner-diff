"""Run the ownership diff end to end and write it out as CSV.

The four repositories arrive injected, so this module names no transport and no
URL: it decides the order the sources are consulted in, which packages the owner
search is asked about, and what a row looks like once written. That order is
load-bearing rather than incidental -- the local input file is read and
validated before either remote is touched, so a mistyped ``-i`` costs no network
round-trips at all. Every exception the repositories and the parser raise
travels straight through -- the CLI owns the mapping from failure to exit code,
and catching anything here would put a second, quieter copy of that decision
inside the run.

One thing is validated here and nowhere else: the two owner cells. Owner names
come from two untrusted remote documents and no earlier stage has ever looked at
them. The other two cells are not in that position. The status cell is a
:class:`~bugowner_diff.domain.status_row.Status` member. The package cell came
from the operator's own input file through the allowlist in
:mod:`bugowner_diff.repositories.package_list_repository` -- which is a
*different* alphabet, ``[A-Za-z0-9._+-]``, and does permit a leading ``+`` or
``-``. What settles the package cell is therefore provenance and not shape: a
file the operator wrote is not a remote document, and re-checking it here is the
duplication this project refuses. A single allowlist over owner names is what
makes the rest of the writer as plain as it looks:

- It rejects the characters a spreadsheet reads as the start of a formula:
  ``=``, ``+`` and ``@`` anywhere in a name, and ``-`` in the first position,
  which is the only place it turns a cell into an arithmetic expression. It
  rejects the characters that split a cell or a row (comma, quote, TAB, CR, LF)
  and the bidi controls such as U+202E that rewrite the line a reader sees. One
  predicate, four failure classes.
- It also rejects a lone surrogate. ``json.loads`` accepts ``"\\ud800bad"``, and
  the parser keeps such a name unchanged on purpose, because sanitizing it there
  would break the exact set equality the whole diff rests on. Unchecked it would
  reach ``encode`` and raise a bare ``UnicodeEncodeError`` from outside this
  project's error taxonomy; the allowlist reaches it first and says where it
  came from.

So :mod:`csv` needs no quoting configuration, no formula-prefixing and no
force-quote helper: no cell that would trigger quoting can survive to be
written. There is no ``UnicodeEncodeError`` arm either -- after the check no cell
holds a non-ASCII character, so the encode cannot fail on data, and that arm
would be unreachable code.
"""

import contextlib
import csv
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from bugowner_diff.domain.status_row import StatusRow
from bugowner_diff.exceptions import DataSourceError
from bugowner_diff.repositories.obs_owner_repository import ObsOwnerRepository
from bugowner_diff.repositories.obs_project_repository import ObsProjectRepository
from bugowner_diff.repositories.package_list_repository import PackageListRepository
from bugowner_diff.repositories.remote_archive_repository import RemoteArchiveRepository
from bugowner_diff.services.maintainership_snapshot import parse_tagged_snapshot
from bugowner_diff.services.status_service import classify

# Measured over all 226 unique owner names the two sources answered with: the
# character set is exactly [A-Za-z0-9._-], every name starts with a letter, no
# name holds a colon, and lengths run from 3 to 31. The colon is permitted
# anyway because a tagged group name carries the prefix
# `bugowner_diff.domain.owner_name.tag_group` puts there. That prefix is
# deliberately not spelled in this module: the tag rule has exactly one
# definition, and a second spelling of it here is how the two copies drift.
#
# The first character is spelled separately so that `-` is excluded from it. A
# single class ending in `-` would accept `-1-2`, which is the formula lead this
# check exists to refuse, and the hyphen has to stay legal everywhere else
# because ordinary names are full of it.
_RENDERABLE_OWNER_NAME = re.compile(r"[A-Za-z0-9._:][A-Za-z0-9._:-]*")

_HEADER = ["package", "15", "16", "status"]


def run_diff(
    *,
    package_list: PackageListRepository,
    project_repository: ObsProjectRepository,
    owner_repository: ObsOwnerRepository,
    archive_repository: RemoteArchiveRepository,
    input_path: Path,
    output_path: Path | None,
    project: str,
    ref: str,
) -> None:
    """Diff the ownership of every package in the input file and write the report.

    Args:
        package_list: Reader of the input file, which settles both the packages
            to diff and the order they are reported in.
        project_repository: Source of the project listing. Its answer is used as
            a membership test and for nothing else.
        owner_repository: Source of the 15-side owners, asked only about
            packages the listing holds -- that conditional is what keeps the
            measured run at 88 network round-trips instead of 119, and it is the
            only place "absent on the 15 side" is established.
        archive_repository: Source of the SLFO maintainership document.
        input_path: Path to the package list, passed through unread.
        output_path: Destination file, or None to write to stdout.
        project: Project whose listing and owners the 15 column reports. No
            default here: a default is a command-line concern and belongs beside
            the argument parser that offers it.
        ref: Git ref of the maintainership document. No default, for the same
            reason.

    Returns:
        None. Success is having written the report without raising. A failure
        writes nothing at all: the report is rendered in full before the
        destination is opened, so the two outcomes are a complete report and no
        report, never a truncated one that reads as complete.

    Raises:
        DataSourceError: If an owner name cannot be rendered in a CSV cell, and
            -- raised by the sources, not here -- if a remote answered with
            something unusable.
        InputError: If the input file is unusable. Raised by the package list.
        MissingBinaryError: If ``osc`` or ``git`` is not on ``PATH``.
        NetworkTimeoutError: If a remote outlives its source's timeout.
        OSError: If the input or the output file cannot be used. Every one of
            these travels unchanged to the caller, which owns the mapping from
            failure to exit code.
    """
    packages = package_list.load(input_path)
    listing = project_repository.list_packages(project)
    snapshot = parse_tagged_snapshot(archive_repository.fetch_maintainership(ref))
    # Every row is rendered before the destination is opened, so a refused owner
    # name ends the run having written nothing. Rendering as we write would leave
    # a header and the rows up to the bad one on disk -- a truncated report that
    # reads as a finished one, which is the partial answer this project refuses.
    # The whole report is a few hundred rows, so holding it costs nothing worth
    # weighing against that.
    rows: list[list[str]] = []
    for package in packages:
        sle15_owners = owner_repository.find_owners(package) if package in listing else None
        # `snapshot.get(package)` carries no default, and that is the second half
        # of the contract `classify` is written against: a `frozenset()` default
        # would collapse "the document has no entry" into "the document names
        # nobody", the DROPPED rung would never fire, and the one dropped package
        # of the measured run would report as `outdated` -- a plausible,
        # complete, wrong answer.
        rows.append(_cells(classify(package, sle15_owners, snapshot.get(package))))
    with _open_output(output_path) as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(_HEADER)
        writer.writerows(rows)


@contextlib.contextmanager
def _open_output(path: Path | None) -> Iterator[TextIO]:
    """Yield the CSV destination, closing it only when this function opened it.

    Args:
        path: Destination file, or None to write to stdout.

    Yields:
        A writable text stream. stdout is yielded as-is and never closed, since
        the process still needs it after the command returns.

    Note:
        The two branches do not agree on encoding: a file is written as UTF-8,
        while stdout keeps whatever codec the locale gave it. No cell can
        observe the difference, because :data:`_RENDERABLE_OWNER_NAME` leaves
        nothing non-ASCII to encode. Reconfiguring stdout to close the gap would
        mutate process-global state the logging handlers on stderr also observe,
        for a case no cell can reach.
    """
    if path is None:
        yield sys.stdout
        return
    # newline="" is the csv module's documented contract for the file it writes
    # to. On POSIX it is not observable -- write-side translation maps \n to
    # os.linesep, which is already \n -- but it is what stops the platform from
    # reintroducing \r wherever os.linesep differs.
    with path.open("w", newline="", encoding="utf-8") as handle:
        yield handle


def _cells(row: StatusRow) -> list[str]:
    """Render one row as its four CSV cells, in the row's own field order.

    ``status`` needs no conversion: :class:`~bugowner_diff.domain.status_row.Status`
    is a ``StrEnum``, so a member already *is* the cell it writes.
    """
    return [
        row.package,
        _owner_cell(row.package, row.sle15_owners, column="15", source="the OBS owner search"),
        _owner_cell(row.package, row.slfo_maintainers, column="16", source="_maintainership.json"),
        row.status,
    ]


def _owner_cell(package: str, names: frozenset[str], *, column: str, source: str) -> str:
    """Join one side's owners into a cell, refusing a name a cell cannot hold.

    Args:
        package: Package the names belong to, named in the complaint so a reader
            knows which line of which document to go and look at.
        names: Tagged owner names. An empty set joins to an empty cell, which is
            how an absent side is reported.
        column: ``"15"`` or ``"16"``, as the header spells it.
        source: Where the names came from, for the same reason as ``package``.

    Returns:
        The names sorted and joined with a space. Sorting is required rather
        than cosmetic: the row holds frozensets, whose iteration order depends on
        the hash seed, so an unsorted cell would differ between two runs over
        identical data.

    Raises:
        DataSourceError: If a name leaves the allowlist this module's docstring
            explains.
    """
    ordered = sorted(names)
    for name in ordered:
        if _RENDERABLE_OWNER_NAME.fullmatch(name) is None:
            # !r on the name, as everywhere this project quotes remote text
            # back: repr escapes a lone surrogate, which a bare {name} would
            # instead try to encode on its way to the terminal.
            raise DataSourceError(
                f"Owner name in the {column} column of package {package!r} cannot be rendered "
                f"in a CSV cell: expected a non-empty [A-Za-z0-9._:-] that does not start with "
                f"'-', got {name!r} from {source}."
            )
    return " ".join(ordered)
