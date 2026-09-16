"""Tests for the package-list input file repository."""

import contextlib
import os
import threading
from pathlib import Path

import pytest

from bugowner_diff.repositories.package_list_repository import (
    MAX_PACKAGE_LIST_BYTES,
    MAX_PACKAGE_NAME_LENGTH,
    PackageListRepository,
    PackageListRepositoryImpl,
)


def _write(tmp_path: Path, content: str) -> Path:
    """Write an input file and return its path."""
    file_path = tmp_path / "packages.txt"
    file_path.write_text(content, encoding="utf-8")
    return file_path


def test_the_implementation_satisfies_the_repository_protocol() -> None:
    # mypy is scoped to src/, so nothing type-checks the conformance of this
    # Impl to the Protocol the command layer will inject against. What this
    # buys is narrow but real: isinstance against a runtime_checkable Protocol
    # compares attribute names only, so it catches a renamed method and not a
    # changed signature.
    assert isinstance(PackageListRepositoryImpl(), PackageListRepository)


def test_load_returns_the_package_names_in_file_order(tmp_path: Path) -> None:
    # Order is the CSV row order, so it is part of the contract, not an
    # accident of the reader.
    file_path = _write(tmp_path, "SDL3\nethtool\nspice\ncrmsh\n")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool", "spice", "crmsh"]


def test_load_strips_whitespace_around_each_name(tmp_path: Path) -> None:
    # An unstripped name fails the allowlist and would abort a run over a file
    # a human hand-edited.
    file_path = _write(tmp_path, "  SDL3\nethtool  \n\tspice\t\n")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool", "spice"]


def test_load_skips_lines_that_are_empty_after_stripping(tmp_path: Path) -> None:
    # A blank line is not a package, and a trailing newline always produces one.
    file_path = _write(tmp_path, "\nSDL3\n\n   \n\t\nethtool\n\n")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool"]


def test_load_keeps_only_the_first_occurrence_of_a_repeated_name(tmp_path: Path) -> None:
    # A duplicate would query OBS twice and emit two identical CSV rows.
    file_path = _write(tmp_path, "SDL3\nethtool\nSDL3\nspice\nethtool\n")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool", "spice"]


def test_load_does_not_treat_a_byte_order_mark_as_part_of_the_first_name(tmp_path: Path) -> None:
    # An editor-written BOM would otherwise make only the first name fail the
    # allowlist, which reads as a defect in that one package.
    file_path = tmp_path / "packages.txt"
    file_path.write_text("SDL3\nethtool\n", encoding="utf-8-sig")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool"]


def test_load_does_not_keep_the_carriage_return_of_crlf_line_endings(tmp_path: Path) -> None:
    # The bytes are written raw, so nothing but strip() can remove the \r. This
    # is the only test that would fail if strip() were ever narrowed to
    # strip(" \t"), which would leave every name on a CRLF file unusable.
    file_path = tmp_path / "packages.txt"
    file_path.write_bytes(b"SDL3\r\nethtool\r\n")

    assert PackageListRepositoryImpl().load(file_path) == ["SDL3", "ethtool"]


def test_load_returns_an_empty_list_for_an_empty_file(tmp_path: Path) -> None:
    # Nothing to diff is not an error here; the command layer decides what an
    # empty run means.
    assert PackageListRepositoryImpl().load(_write(tmp_path, "")) == []


@pytest.mark.parametrize(
    "name",
    [
        "SDL3&ethtool",
        "SDL3?x=1",
        "SDL3%2f",
        "sub/SDL3",
        "project:SDL3",
        "SDL3 extra",
        "SDL\tx",
        "SDL3'",
        'SDL3"',
        "SDL3;rm",
        "SDL3|x",
        "SDL3$x",
        "SDL3`x`",
        "SDL3\x00",
        "SDL3\x07",
        "SDL3\x1b[0m",
        "ünïcode",
    ],
    ids=[
        "ampersand",
        "question-mark",
        "percent",
        "slash",
        "colon",
        "inner-space",
        "inner-tab",
        "single-quote",
        "double-quote",
        "semicolon",
        "pipe",
        "dollar",
        "backtick",
        "nul",
        "bell",
        "escape",
        "non-ascii",
    ],
)
def test_load_rejects_a_name_that_leaves_the_allowlist(tmp_path: Path, name: str) -> None:
    # Every name returned here is interpolated into an OBS query string and
    # handed to a subprocess argv, so the allowlist is the chokepoint.
    file_path = _write(tmp_path, f"SDL3\n{name}\n")

    with pytest.raises(ValueError):
        PackageListRepositoryImpl().load(file_path)


def test_load_names_the_line_number_and_the_offending_name_when_it_rejects(
    tmp_path: Path,
) -> None:
    # The line number counts physical lines, blanks included, so it points at
    # what the user sees in an editor.
    file_path = _write(tmp_path, "SDL3\n\nethtool\nbad name\nspice\n")

    with pytest.raises(ValueError) as excinfo:
        PackageListRepositoryImpl().load(file_path)

    message = str(excinfo.value)
    assert "4" in message
    assert repr("bad name") in message


@pytest.mark.parametrize(
    "separator",
    ["\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
    ids=["vtab", "formfeed", "fs", "gs", "rs", "nel", "line-sep", "paragraph-sep"],
)
def test_load_treats_a_line_splitter_other_than_newline_as_part_of_the_name(
    tmp_path: Path, separator: str
) -> None:
    # str.splitlines() splits on all of these, so one physical line would
    # silently yield two names that both pass the allowlist. Written as escapes,
    # never as literal characters, so no editor can normalize one case into a
    # silent duplicate of another. Fail closed: the separator stays inside the
    # name, leaves the allowlist and aborts the load.
    file_path = _write(tmp_path, f"SDL3{separator}ethtool\n")

    with pytest.raises(ValueError) as excinfo:
        PackageListRepositoryImpl().load(file_path)

    message = str(excinfo.value)
    # The whole line is reported as one name, which is what proves the
    # separator was not treated as a line break. Asserting the repr rather than
    # just the line number, because the repr of several of these separators
    # itself contains a "1" and would satisfy a laxer check for the wrong
    # reason.
    assert repr(f"SDL3{separator}ethtool") in message
    assert "line 1" in message


def test_load_accepts_a_name_of_the_maximum_allowed_length(tmp_path: Path) -> None:
    name = "a" * MAX_PACKAGE_NAME_LENGTH

    assert PackageListRepositoryImpl().load(_write(tmp_path, name)) == [name]


def test_load_rejects_a_name_one_character_over_the_maximum_length(tmp_path: Path) -> None:
    file_path = _write(tmp_path, "a" * (MAX_PACKAGE_NAME_LENGTH + 1))

    with pytest.raises(ValueError):
        PackageListRepositoryImpl().load(file_path)


def test_load_accepts_every_character_class_the_allowlist_permits(tmp_path: Path) -> None:
    # Real package names carry dots, plus signs, underscores and dashes
    # (abseil-cpp, libfoo1.2, gtk+); the allowlist must not be tighter than
    # the packages it has to pass.
    file_path = _write(tmp_path, "abseil-cpp\nlibfoo1.2\ngtk+\npy_mod\nSDL3\n")

    assert PackageListRepositoryImpl().load(file_path) == [
        "abseil-cpp",
        "libfoo1.2",
        "gtk+",
        "py_mod",
        "SDL3",
    ]


def _file_of_exactly(tmp_path: Path, total_bytes: int) -> Path:
    """Write a file of valid names measuring exactly ``total_bytes`` bytes."""
    # One name per line, so only the size can be what a rejection is about. The
    # last line is short rather than newline-terminated, which lets the total
    # land on an exact byte count for any target.
    line = "a" * 9 + "\n"
    whole, remainder = divmod(total_bytes, len(line))
    content = line * whole + "a" * remainder
    file_path = _write(tmp_path, content)
    assert file_path.stat().st_size == total_bytes
    return file_path


def test_load_reads_a_file_of_exactly_the_byte_cap(tmp_path: Path) -> None:
    # Exactly at the cap, not near it: pins `>` rather than `>=`, so tightening
    # the comparison cannot start rejecting files that used to be accepted.
    file_path = _file_of_exactly(tmp_path, MAX_PACKAGE_LIST_BYTES)

    assert PackageListRepositoryImpl().load(file_path) == ["a" * 9, "a" * 6]


def test_load_refuses_a_file_one_byte_over_the_byte_cap(tmp_path: Path) -> None:
    file_path = _file_of_exactly(tmp_path, MAX_PACKAGE_LIST_BYTES + 1)

    with pytest.raises(ValueError) as excinfo:
        PackageListRepositoryImpl().load(file_path)

    # The user cannot act on the refusal without being told the limit.
    assert str(MAX_PACKAGE_LIST_BYTES) in str(excinfo.value)


def test_load_bounds_the_read_of_a_file_whose_reported_size_is_zero(tmp_path: Path) -> None:
    # A FIFO, /dev/stdin and /proc files all report st_size == 0, so a cap
    # tested against stat() never fires for exactly the inputs whose length it
    # cannot know. A named pipe is the portable stand-in: the writer pushes far
    # more than the cap, and load must refuse instead of reading to EOF.
    fifo_path = tmp_path / "packages.fifo"
    os.mkfifo(fifo_path)
    payload = ("a" * 9 + "\n") * (MAX_PACKAGE_LIST_BYTES // 10 * 2)

    def feed() -> None:
        # Opening for write blocks until load opens the read end. suppress is
        # outermost so it also covers the close(), which raises once load has
        # stopped reading at the cap and closed its end.
        with (
            contextlib.suppress(BrokenPipeError),
            open(fifo_path, "w", encoding="utf-8") as handle,
        ):
            handle.write(payload)

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        assert fifo_path.stat().st_size == 0

        with pytest.raises(ValueError) as excinfo:
            PackageListRepositoryImpl().load(fifo_path)

        assert str(MAX_PACKAGE_LIST_BYTES) in str(excinfo.value)
    finally:
        writer.join(timeout=5)


def test_load_truncates_the_rejected_name_it_echoes_back(tmp_path: Path) -> None:
    # The message reaches stderr and, once -v/-d exist, a log. Pointing -i at a
    # credential file must report the mistake without reproducing the line: the
    # cap bounds a rejected line only at the size of the whole file.
    # Stands in for a line of ~/.netrc or a private key: long, and rejected by
    # the allowlist on its first "=". Written as filler rather than as a
    # credential-shaped literal, so this test does not itself trip a secret
    # scanner on every commit.
    long_line = "k" * 40 + "=" + "v" * 500
    file_path = _write(tmp_path, f"SDL3\n{long_line}\n")

    with pytest.raises(ValueError) as excinfo:
        PackageListRepositoryImpl().load(file_path)

    message = str(excinfo.value)
    assert long_line not in message
    assert len(message) < len(long_line)
    # Still enough of the name to recognise which line went wrong.
    assert long_line[:40] in message


def test_load_rejects_a_file_that_is_not_valid_utf_8(tmp_path: Path) -> None:
    # utf-8-sig is a deliberate choice, so its failure mode is part of the
    # contract. UnicodeDecodeError is a ValueError subclass, which is what lets
    # the taxonomy stay at three exceptions.
    file_path = tmp_path / "packages.txt"
    file_path.write_bytes(b"SDL3\nspi\xe7e\n")

    with pytest.raises(ValueError):
        PackageListRepositoryImpl().load(file_path)


def test_load_leaves_a_read_failure_as_an_os_error_for_the_caller(tmp_path: Path) -> None:
    # Deliberately unwrapped rather than translated: the exit-code ladder maps
    # exception type to exit code, so wrapping this in ValueError here would
    # merge "bad input file" with "bad name inside a good file".
    with pytest.raises(OSError):
        PackageListRepositoryImpl().load(tmp_path / "does-not-exist.txt")
