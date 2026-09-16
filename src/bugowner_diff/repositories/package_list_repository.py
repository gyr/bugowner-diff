"""Read the user-supplied package list into an ordered, unique list of names.

Every name this module returns is later interpolated into an OBS owner-search
query string and handed to a subprocess argv. Nothing downstream re-validates,
so the allowlist here is the chokepoint for ``&``, ``?``, ``%``, ``/``, ``:``,
quotes, whitespace and control characters.

A name outside the allowlist aborts the load instead of being skipped: the
report is specified to carry one row per input package, so a dropped name would
silently shorten the CSV and read as a successful run.
"""

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

MAX_PACKAGE_NAME_LENGTH = 200

# 64 KiB. A conforming line is at most MAX_PACKAGE_NAME_LENGTH characters plus
# CRLF, so the cap still admits ~324 worst-case lines and ~3200 realistic ones,
# against the 119 short lines of the real input. Bounds the read of a corrupted
# or hostile file -- the whole text is held in memory to report line numbers.
MAX_PACKAGE_LIST_BYTES = 64 * 1024

# How much of a rejected name the error message echoes. A rejected line is
# bounded only by the file cap, and the message reaches stderr and, once -v/-d
# arrive, a log file. Pointing -i at ~/.netrc or an ssh key should report the
# mistake without reproducing a credential line.
_MAX_SHOWN_NAME_LENGTH = 80

# Built from the length constant so the two cannot drift apart. fullmatch, not
# match with `$`, which also matches just before a terminal newline. Splitting
# on `\n` and stripping already makes a trailing newline unreachable here, so
# this guards a future edit that drops the strip rather than a live defect.
_VALID_PACKAGE_NAME = re.compile(rf"[A-Za-z0-9._+-]{{1,{MAX_PACKAGE_NAME_LENGTH}}}")


@runtime_checkable
class PackageListRepository(Protocol):
    """Load the package names to diff from an input file."""

    def load(self, file_path: Path) -> list[str]:
        """Return the package names held by the file, in first-seen order.

        Args:
            file_path: Path to the input file. Relative paths are accepted;
                the path comes straight from ``-i`` on the command line.

        Returns:
            The package names, stripped, without blanks and without repeats.

        Raises:
            ValueError: If the file exceeds the implementation's size limit, is
                not valid UTF-8, or a name leaves the ``[A-Za-z0-9._+-]``
                allowlist or ``MAX_PACKAGE_NAME_LENGTH`` characters. A decode
                failure arrives as ``UnicodeDecodeError``, a ``ValueError``
                subclass, and is the one rejection reported with a byte offset
                instead of a line number.
            OSError: If the file cannot be opened or read. Left unwrapped for
                the caller, which owns the mapping from failure to exit code.
        """
        ...


class PackageListRepositoryImpl:
    """Adapter implementation backed by a newline-separated text file."""

    def load(self, file_path: Path) -> list[str]:
        """Read, strip, validate and deduplicate the names in ``file_path``.

        The contract -- arguments, return value and the exceptions raised -- is
        stated once, on :meth:`PackageListRepository.load`. Restating it here
        would give the two halves room to drift.
        """
        # Bounded read of the open descriptor, rather than stat() followed by
        # read_text(). A FIFO, /dev/stdin or a /proc file reports st_size == 0,
        # so a cap tested against stat() never fires for precisely the inputs
        # whose length it cannot know. Reading one byte past the cap also closes
        # the window in which a regular file grows between the two calls. The
        # size is dropped from the message: it was never actionable, and stat()
        # was not telling the truth about it anyway.
        with file_path.open("rb") as handle:
            data = handle.read(MAX_PACKAGE_LIST_BYTES + 1)
        if len(data) > MAX_PACKAGE_LIST_BYTES:
            raise ValueError(
                f"package list exceeds {MAX_PACKAGE_LIST_BYTES} bytes; refusing to read it"
            )

        # utf-8-sig, not utf-8: an editor-written BOM would otherwise be part of
        # the first name only, and read as a defect in that one package.
        text = data.decode("utf-8-sig")

        names: list[str] = []
        seen: set[str] = set()
        # split("\n"), not splitlines(): splitlines() also breaks on \x0b, \x0c,
        # \x1c, \x1d, \x1e, \x85, \u2028 and \u2029, so one physical line could
        # silently yield two names that both pass the allowlist, and every later
        # line number in an error message would be shifted. Splitting on \n
        # alone keeps such a separator between two names inside one name, where
        # the allowlist rejects it. A separator at the very end of a line is
        # instead removed by strip(), which yields the same single name and so
        # still manufactures nothing. The trailing \r of a CRLF file goes the
        # same way.
        #
        # Numbering physical lines, blanks included, so it points at what the
        # user sees in an editor.
        for line_number, line in enumerate(text.split("\n"), start=1):
            name = line.strip()
            if not name:
                continue
            if not _VALID_PACKAGE_NAME.fullmatch(name):
                shown = name[:_MAX_SHOWN_NAME_LENGTH]
                if len(name) > _MAX_SHOWN_NAME_LENGTH:
                    shown += "..."
                # !r, not the bare name: repr escapes control characters, so a
                # name carrying ANSI or a bidi override cannot rewrite the
                # terminal line the user reads the complaint on.
                raise ValueError(
                    f"invalid package name on line {line_number}: {shown!r}; "
                    f"expected [A-Za-z0-9._+-] up to {MAX_PACKAGE_NAME_LENGTH} characters"
                )
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names
