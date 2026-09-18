"""Tests for the remote git archive repository."""

import gzip
import io
import os
import subprocess
import tarfile

import pytest

from bugowner_diff.exceptions import (
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)
from bugowner_diff.repositories import remote_archive_repository
from bugowner_diff.repositories.remote_archive_repository import (
    GIT_TIMEOUT_SECONDS,
    RemoteArchiveRepository,
    RemoteArchiveRepositoryImpl,
)

REF = "main"
DOCUMENT_NAME = "_maintainership.json"
DOCUMENT = b'{"packages": {}}'


def _tar(entries: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    """Build a tar archive in memory, as ``git archive`` would emit it."""
    buffer = io.BytesIO()
    # mode="w", never "w:gz": the fixtures have to be the uncompressed stream
    # git archive really produces, or the module's mode="r:" is never exercised.
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, payload in entries:
            tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def _regular(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
    """Describe one regular-file tar member."""
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


def _typed(tar_type: bytes, linkname: str = "") -> tuple[tarfile.TarInfo, bytes]:
    """Describe one empty tar member of ``tar_type``, named like the document."""
    info = tarfile.TarInfo(DOCUMENT_NAME)
    info.type = tar_type
    info.linkname = linkname
    return info, b""


MAINTAINERSHIP_TAR = _tar([_regular(DOCUMENT_NAME, DOCUMENT)])


class _RecordingGit:
    """Stand in for :func:`subprocess.run`, recording calls and replaying one result."""

    def __init__(
        self,
        stdout: bytes = MAINTAINERSHIP_TAR,
        returncode: int = 0,
        stderr: bytes = b"",
        raises: BaseException | None = None,
    ) -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool = False,
        check: bool = False,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((argv, {"capture_output": capture_output, "check": check, **kwargs}))
        if self.raises is not None:
            raise self.raises
        # Honoured, not merely recorded, for the reason the sibling repository's
        # fake spells out: without capture_output the real subprocess.run leaves
        # stdout None, and with check=True it raises CalledProcessError, which is
        # outside both BugownerDiffError and OSError and so escapes the taxonomy.
        stdout = self.stdout if capture_output else None
        stderr = self.stderr if capture_output else None
        if check and self.returncode != 0:
            raise subprocess.CalledProcessError(self.returncode, argv, stdout, stderr)
        return subprocess.CompletedProcess(argv, self.returncode, stdout, stderr)


def _install_git(monkeypatch: pytest.MonkeyPatch, git: _RecordingGit) -> _RecordingGit:
    """Replace the subprocess this module shells out through."""
    monkeypatch.setattr(remote_archive_repository.subprocess, "run", git)
    return git


def test_fetch_maintainership_returns_the_document_out_of_the_tar_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The whole point of the module: the bytes of the one member, not the tar
    # around them, and never a path on disk -- nothing is cloned, nothing is
    # written out and extractall is never called. Parsing the JSON is the next
    # commit's job, so the raw bytes are what the contract promises.
    _install_git(monkeypatch, _RecordingGit())

    assert RemoteArchiveRepositoryImpl().fetch_maintainership(REF) == DOCUMENT


def test_fetch_maintainership_asks_git_for_one_file_at_the_ref_it_was_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A list, never a shell string, and the pathspec sits after "--" so a ref and
    # a path can never be confused for one another. The ref does occupy an argv
    # slot of its own -- git archive takes it nowhere else -- which is why the
    # validation above it refuses a leading dash rather than merely quoting it.
    git = _install_git(monkeypatch, _RecordingGit())

    RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    argv, _ = git.calls[0]
    assert argv == [
        "git",
        "archive",
        "--remote=gitea@src.suse.de:products/SLFO.git",
        REF,
        "--",
        DOCUMENT_NAME,
    ]


def test_fetch_maintainership_pins_the_transport_and_denies_git_a_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The security properties of this module live entirely in these four kwargs,
    # and every one of them dies silently if it regresses -- the happy path is
    # green without any of them.
    #
    # GIT_ALLOW_PROTOCOL pinned to ssh alone is the only lever that holds: it
    # overrides the operator's gitconfig, an inherited GIT_ALLOW_PROTOCOL and an
    # inherited GIT_CONFIG_COUNT alike, where `-c protocol.ext.allow=never` loses
    # to the environment variable. It is also the only defence against a
    # url.<x>.insteadOf rewrite turning this URL into an ext:: one *inside* git,
    # which is command execution. ssh is measured to be the only transport that
    # reaches this remote anyway -- https prompts for credentials and fails.
    #
    # GIT_TERMINAL_PROMPT=0 and /dev/null are the pair that makes an
    # unauthenticated remote fail fast instead of blocking on a credential prompt
    # nobody is watching for, and the deadline is what makes NetworkTimeoutError
    # reachable at all. os.environ is carried through because ssh needs the
    # operator's HOME, SSH_AUTH_SOCK and PATH to authenticate at all.
    git = _install_git(monkeypatch, _RecordingGit())

    RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    _, kwargs = git.calls[0]
    assert kwargs["env"] == {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "ssh",
    }
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == GIT_TIMEOUT_SECONDS
    assert GIT_TIMEOUT_SECONDS > 0


@pytest.mark.parametrize(
    "ref",
    ["--upload-pack=touch /tmp/pwned", "", "main\n", "refs/heads/feature;id", "café"],
    ids=["leading-dash", "empty", "trailing-newline", "shell-token", "non-ascii"],
)
def test_fetch_maintainership_refuses_a_ref_before_it_reaches_git(
    monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    # The ref comes from --ref on the command line and occupies an argv slot of
    # its own, so the leading character class is doing real work: a value
    # starting with a dash is read by git as an option, and --upload-pack= names
    # a program to run on the far side. fullmatch and not match, because "$" also
    # matches just before a terminal newline -- re.match(r"[\w./-]+$", "main\n")
    # succeeds and the allowlist leaks.
    #
    # Refused *before* the subprocess, which the assertion on the call log pins:
    # a rejected ref that has already been handed to git has not been refused at
    # all. One regex and one message covers all five -- whatever else the CLI can
    # be handed dies at the remote's exit code, not at a branch of its own here.
    #
    # "café" is why the class is spelled out rather than written \w: \w on a str
    # pattern is unicode-aware, so it admits every letter and digit in Unicode
    # while the message promises [A-Za-z0-9_]. Nothing is injectable through it,
    # but a ref the contract says is refused has to be refused here and not come
    # back from the remote as a DataSourceError about something else.
    git = _install_git(monkeypatch, _RecordingGit())

    with pytest.raises(InputError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(ref)

    assert repr(ref) in str(caught.value)
    assert git.calls == []


def test_fetch_maintainership_reports_a_missing_binary_when_git_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # git is not a dependency this package can install for the user, so "not on
    # PATH" is a setup mistake with its own exit code and its own remedy. One
    # check, not two: shutil.which before the call would ask the same question
    # the exec already answers, and the two can disagree.
    _install_git(monkeypatch, _RecordingGit(raises=FileNotFoundError(2, "No such file", "git")))

    with pytest.raises(MissingBinaryError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert caught.value.binary == "git"


def test_fetch_maintainership_lets_an_unattributable_os_error_through_unwrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A git that is present but not executable, a fork that hits RLIMIT_NPROC, a
    # /dev/null that cannot be opened: errno already says more than any wrapper
    # could add, and reporting one of these as a missing binary would tell the
    # user to install something that is already installed. The taxonomy's rule is
    # that OSError escapes by design, and a bare `except OSError` around the
    # subprocess call would silently break it -- FileNotFoundError and
    # PermissionError are both OSError subclasses, so only the narrow catch keeps
    # the two apart.
    _install_git(monkeypatch, _RecordingGit(raises=PermissionError(13, "Permission denied", "git")))

    with pytest.raises(PermissionError):
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)


def test_fetch_maintainership_reports_a_timeout_when_git_outlives_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A separate exit code from a generic failure, because the remedy differs:
    # wait and re-run. The deadline travels with the error so a slow link can be
    # told from an ssh that is wedged on a host key nobody will ever confirm.
    _install_git(
        monkeypatch,
        _RecordingGit(raises=subprocess.TimeoutExpired(cmd=["git"], timeout=GIT_TIMEOUT_SECONDS)),
    )

    with pytest.raises(NetworkTimeoutError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert caught.value.timeout == GIT_TIMEOUT_SECONDS


def test_fetch_maintainership_reports_what_git_said_when_it_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every non-zero exit, with no exception: an unknown ref and a file that does
    # not exist at that ref are indistinguishable here from a rejected key or an
    # ssh that never connected, and git's own stderr is the whole of the
    # diagnosis. The alternative is matching that stderr for "no such ref" or
    # "did not match any files", which are gettext-translated by the *remote's*
    # locale -- LC_ALL here would not change them -- and getting the match wrong
    # turns a transport failure into a confident lie about the ref. stderr is
    # reproduced with !r because it is remote-influenced text on its way to a
    # terminal.
    _install_git(
        monkeypatch,
        _RecordingGit(
            stdout=b"", returncode=128, stderr=b"remote: fatal: no such ref: nonexistent\n"
        ),
    )

    with pytest.raises(DataSourceError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert "128" in str(caught.value)
    assert repr(REF) in str(caught.value)
    assert "no such ref" in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [b"not a tar at all", MAINTAINERSHIP_TAR[:512], gzip.compress(MAINTAINERSHIP_TAR)],
    ids=["not-a-tar", "truncated", "gzip-compressed"],
)
def test_fetch_maintainership_refuses_a_body_that_is_not_a_readable_uncompressed_tar(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    # A git that exits 0 having written something other than the archive is a
    # failure this module has to name, not a traceback: tarfile.TarError is not
    # an OSError -- verified -- so without an arm of its own it escapes the
    # taxonomy entirely.
    #
    # The gzip case is what pins mode="r:". Plain "r" transparently inflates
    # gzip, bz2 and xz, and git archive emits uncompressed tar, so a compressed
    # stream here means the bytes did not come from the command that was run --
    # and accepting it would hand an attacker-controlled decompressor a body this
    # module never bounds.
    _install_git(monkeypatch, _RecordingGit(stdout=body))

    with pytest.raises(DataSourceError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert "tar" in str(caught.value)


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [_regular(DOCUMENT_NAME, DOCUMENT), _regular("other.json", b"{}")],
    ],
    ids=["none", "two"],
)
def test_fetch_maintainership_refuses_an_archive_that_is_not_exactly_one_member(
    monkeypatch: pytest.MonkeyPatch, entries: list[tuple[tarfile.TarInfo, bytes]]
) -> None:
    # The pathspec names one file in the repository root, so one member is the
    # only shape this command produces. An empty archive would otherwise be read
    # as an empty document and a second member would be silently ignored --
    # either way the maintainership side of the diff answers from something that
    # is not the document.
    _install_git(monkeypatch, _RecordingGit(stdout=_tar(entries)))

    with pytest.raises(DataSourceError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert str(len(entries)) in str(caught.value)


@pytest.mark.parametrize(
    "member",
    [_typed(tarfile.SYMTYPE, "/etc/passwd"), _typed(tarfile.GNUTYPE_SPARSE)],
    ids=["symlink", "gnu-sparse"],
)
def test_fetch_maintainership_refuses_a_member_that_is_not_a_plain_regular_file(
    monkeypatch: pytest.MonkeyPatch, member: tuple[tarfile.TarInfo, bytes]
) -> None:
    # A symlink member named _maintainership.json reads whatever it points at
    # once something follows it, and a directory member carries no content at
    # all. Refusing anything but a regular file is also what makes extractfile's
    # Optional impossible rather than merely unlikely.
    #
    # The type is compared to REGTYPE rather than asked isreg(), because
    # REGULAR_TYPES also holds GNUTYPE_SPARSE, CONTTYPE and AREGTYPE, none of
    # which git archive emits. A sparse member is the one that bites: its content
    # length comes from a header field and not from the stream, so a 1,536-byte
    # archive measured here returns 1,048,576 bytes from read(), and the field is
    # free to declare gigabytes. Refusing the type is what keeps the accepted
    # shape equal to the shape the command actually produces.
    _install_git(monkeypatch, _RecordingGit(stdout=_tar([member])))

    with pytest.raises(DataSourceError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert repr(DOCUMENT_NAME) in str(caught.value)


def test_fetch_maintainership_refuses_a_member_that_is_not_the_document_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The name is checked rather than assumed: the one member being the only
    # member does not make it the file the pathspec named, and parsing the wrong
    # document as the maintainership file is a complete, plausible, entirely
    # wrong report rather than a failure.
    _install_git(monkeypatch, _RecordingGit(stdout=_tar([_regular("README.md", DOCUMENT)])))

    with pytest.raises(DataSourceError) as caught:
        RemoteArchiveRepositoryImpl().fetch_maintainership(REF)

    assert "README.md" in str(caught.value)
    assert repr(DOCUMENT_NAME) in str(caught.value)


def test_the_implementation_satisfies_the_remote_archive_repository_protocol() -> None:
    # mypy is scoped to src/, so nothing type-checks the conformance of this Impl
    # to the Protocol the command layer will inject against. isinstance against a
    # runtime_checkable Protocol compares attribute names only, so it catches a
    # renamed method and not a changed signature.
    assert isinstance(RemoteArchiveRepositoryImpl(), RemoteArchiveRepository)
