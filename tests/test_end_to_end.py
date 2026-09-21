"""Two proofs no unit test in this suite can give.

The first is a whole run: `main` down through the four real repositories, the
real XML and JSON parsers and the real `run_diff`, over authored bytes. Every
other test in the suite substitutes a fake at some seam, so none of them can
show that the pieces agree with each other -- that the listing the project
parser returns is the membership test the command expects, that a bare
`<collection/>` survives as an empty set rather than as None, and that the tar
the archive repository unwraps feeds the snapshot parser.

The second is a real closed pipe. `tests/test_cli.py` neutralises `os.dup2`,
because the real call would point pytest's own fd 1 at /dev/null for the rest
of the session -- which means the one thing that pair of calls exists for,
leaving no `Exception ignored on flushing sys.stdout` behind, can only be
observed from outside this process.

Neither test touches the network: the first replaces the stdlib
`subprocess.run` and refuses any command it was not given an answer for, and
the second replaces the run itself, so no repository method is ever called.
`main` does still construct the four repositories to pass them in, and
constructing one does nothing.
"""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from bugowner_diff.cli import main
from bugowner_diff.exit_codes import ExitCode

_PROJECT = "SUSE:SLE-15-SP7:GA"

# `unrelated-package` is in the listing and not in the input file, which is the
# normal case: a real listing holds thousands of names the report never asks
# about. `count` is not decoration -- the parser cross-checks it against the
# entries it read and aborts the run if the two disagree.
_LISTING_XML = b"""<directory count="5">
  <entry name="abseil-cpp"/>
  <entry name="blktrace"/>
  <entry name="catatonit"/>
  <entry name="spice"/>
  <entry name="unrelated-package"/>
</directory>
"""

# One document per package that is in the input file *and* in the listing; no
# other package is ever searched for, and `_fake_run` fails on an unexpected
# one. `catatonit` answers with a bare `<collection/>`, the real answer for 3 of
# the 88 packages probed and the only thing that produces `unmaintained`. The
# `project` and `package` attributes are carried because the real answers carry
# them; the parser inspects neither.
_OWNER_XML = {
    "abseil-cpp": b'<collection><owner project="SUSE:SLE-15:GA" package="abseil-cpp">'
    b'<group name="team-a" role="bugowner"/></owner></collection>',
    "blktrace": b'<collection><owner project="SUSE:SLE-15:GA" package="blktrace">'
    b'<person name="user-a" role="bugowner"/></owner></collection>',
    "catatonit": b"<collection/>",
    "spice": b'<collection><owner project="SUSE:SLE-15:GA" package="spice">'
    b'<group name="team-b" role="bugowner"/></owner></collection>',
}

# `kernel-source` is in the document and not in the input file, and it carries
# no `groups` key. That asymmetry is measured, not tidied away: `users` is
# present in all 2981 real entries and `groups` is absent from 3, and the parse
# loop reaches every entry whatever the input file asks for -- so an entry like
# this one aborted every possible run until commit 9. `header` and `project`
# ride along because the real document has them and the parser ignores them;
# their keys and value shapes are measured too, so nothing here claims the
# document looks like something it does not. The one project-level user is a
# placeholder for the one the real document carries.
_MAINTAINERSHIP_DOCUMENT = json.dumps(
    {
        "header": {"document": "obs-maintainers", "version": "1.0"},
        "project": {"groups": [], "users": ["user-a"]},
        "packages": {
            "abseil-cpp": {"groups": ["team-a"], "users": []},
            "blktrace": {"groups": ["team-a"], "users": ["user-b"]},
            "catatonit": {"groups": [], "users": ["user-c"]},
            "SDL3": {"groups": ["team-a"], "users": []},
            "kernel-source": {"users": ["user-a"]},
        },
    }
).encode()

# The child of the closed-pipe test. It replaces `run_diff` rather than letting
# a real run happen, so the pipe is the only thing under test; `cli` imported
# the name and calls it bare, so rebinding it on the module substitutes it.
#
# The report it writes is deliberately small. Four short rows stay inside
# Python's 8 KiB stdout buffer, so nothing has reached the pipe by the time
# `run_diff` returns, and the `sys.stdout.flush()` on the next line of `main`
# is what meets the departed reader. Flooding the pipe instead would raise
# `BrokenPipeError` inside `run_diff`, and that flush -- half of the pair this
# test exists to prove -- would never execute at all.
#
# `sys.stdin.readline()` is the handshake. It holds the child inside the
# replaced run until the parent has closed the read end, so which process gets
# there first is fixed rather than raced.
#
# `main` is called with no argument on purpose: that is the console script's
# own call, reading sys.argv.
_PIPE_CHILD = """
import sys

from bugowner_diff import cli


def _small_report(**kwargs):
    for row in range(4):
        print(f"package-{row},user-a,user-a,none")
    sys.stdin.readline()


cli.run_diff = _small_report
sys.exit(cli.main())
"""


def _tar_stream() -> bytes:
    """Wrap the maintainership document the way ``git archive`` hands it over.

    Uncompressed, one regular member, named ``_maintainership.json``: all three
    are required by the extractor, which opens the stream with ``mode="r:"``.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo(name="_maintainership.json")
        member.size = len(_MAINTAINERSHIP_DOCUMENT)
        archive.addfile(member, io.BytesIO(_MAINTAINERSHIP_DOCUMENT))
    return buffer.getvalue()


def _fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
    """Answer the three commands the run makes, and refuse anything else.

    The refusal is the proof that no network call escapes: all three transports
    reach the network through this one function, so a command with no answer
    here is a command this test never authorised.
    """
    if args[0] == "osc" and args[4] == f"/source/{_PROJECT}?expand=1":
        return subprocess.CompletedProcess(args, 0, stdout=_LISTING_XML, stderr=b"")
    if args[0] == "osc" and args[4].startswith("/search/owner?package="):
        package = args[4].removeprefix("/search/owner?package=")
        return subprocess.CompletedProcess(args, 0, stdout=_OWNER_XML[package], stderr=b"")
    if args[0] == "git":
        return subprocess.CompletedProcess(args, 0, stdout=_tar_stream(), stderr=b"")
    raise AssertionError(f"unexpected subprocess: {args!r}")


def test_a_whole_run_turns_the_three_sources_into_one_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Six rows over all five statuses, each one pinning a join the unit tests
    # make separately: `abseil-cpp` that a group tagged on the 15 side by the
    # owner parser and on the 16 side by the snapshot parser compare equal;
    # `catatonit` that `<collection/>` survives as an empty set and not as
    # "absent"; `spice` that the listing and the document disagreeing is
    # `removed`; `SDL3` that a package absent from the listing keeps its 16
    # cell; `hiredis` that a package no source knows is still a row.
    input_path = tmp_path / "packages.txt"
    input_path.write_text("abseil-cpp\nblktrace\ncatatonit\nspice\nSDL3\nhiredis\n")
    output_path = tmp_path / "report.csv"
    monkeypatch.setattr(subprocess, "run", _fake_run)

    exit_code = main(
        [
            "-i",
            str(input_path),
            "-o",
            str(output_path),
            "--project",
            _PROJECT,
            "--ref",
            "slfo-main",
        ]
    )

    assert exit_code == ExitCode.OK
    assert output_path.read_text(encoding="utf-8") == (
        "package,15,16,change\n"
        "abseil-cpp,group:team-a,group:team-a,none\n"
        "blktrace,user-a,group:team-a user-b,changed\n"
        "catatonit,,user-c,unmaintained\n"
        "spice,group:team-b,,removed\n"
        "SDL3,,group:team-a,added\n"
        "hiredis,,,added\n"
    )


def test_a_reader_that_closes_the_pipe_leaves_no_exception_on_the_way_out() -> None:
    # A real child, because this is not observable in-process: the fix under
    # test is `sys.stdout.flush()` inside the try *plus* the `os.dup2` in the
    # BrokenPipeError arm, and `tests/test_cli.py` has to neutralise the dup2 to
    # keep pytest's own stdout. Both halves are load-bearing here: without the
    # flush the report is still in the buffer when the arms are done with it, so
    # the interpreter flushes at shutdown, prints `Exception ignored on flushing
    # sys.stdout` and exits 120 -- a status outside ExitCode altogether -- and
    # without the dup2 the same second failure happens even though the flush
    # raised in a place that could handle it.
    #
    # `-i packages.txt` names no file and none is opened: the parser only
    # applies `type=Path`, and the child has replaced the run.
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            _PIPE_CHILD,
            "-i",
            "packages.txt",
            "--project",
            _PROJECT,
            "--ref",
            "slfo-main",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as child:
        # The reader leaves first and the child is released second. The other
        # order would let the child reach its flush while someone is still on
        # the read end, and the report would simply be delivered.
        child.stdout.close()
        child.stdin.close()
        # Bounded here rather than after the stderr read: `read()` returns only
        # at EOF, so a child that deadlocked would hang on it and never reach a
        # timeout placed below. The one line of stderr is far under the pipe
        # buffer, so collecting it after the child has exited cannot deadlock
        # either.
        returncode = child.wait(timeout=60)
        stderr = child.stderr.read().decode()

    assert "Exception ignored" not in stderr
    # Says the child went out through the BrokenPipeError arm, rather than
    # dying quietly some other way and passing the assertion above by accident.
    assert "Output pipe closed" in stderr
    assert returncode == ExitCode.ERROR
