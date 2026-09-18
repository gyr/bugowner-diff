"""Read the SLFO maintainership document out of its git remote, without cloning.

This is the SLFO side of the diff: the owner set this document records for a
package is compared, as exact set equality on tagged names, against the one the
OBS owner search gives. Parsing it is the next module's job -- what this one
promises is the bytes.

``git archive --remote=<url> <ref> -- <path>`` is the whole of the transport. It
asks the remote for one file and answers with a tar stream on stdout, which is
read here in memory: nothing is cloned, nothing is written to disk and
``extractall`` is never called. The measured document is ~295 KB and 2981
packages, so a clone of the whole product repository to read one file would be
the expensive way round.

ssh is the only transport this reaches the remote over -- measured: https
prompts for credentials and fails -- and the environment pins it, for the
reasons :func:`_fetch_archive` gives.

The remote URL and the file name are literals in :func:`_fetch_archive` rather
than parameters. There is exactly one call site and one document; a general
three-argument fetcher would be an abstraction over a single case, and the
sibling project's is where the ref-validation rules drifted into two
non-identical copies.
"""

import io
import os
import re
import subprocess
import tarfile
from typing import Protocol, runtime_checkable

from bugowner_diff.exceptions import (
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)

# One minute for one small file over ssh. The document is ~295 KB, so this is
# not a deadline scaled to the payload; its job is to make a git that never
# returns fail instead of hang -- an ssh wedged on an unreachable host or a
# host key nobody will confirm would otherwise wait forever.
GIT_TIMEOUT_SECONDS = 60

# The ref reaches git as an argv slot of its own, which the leading character
# class is what protects: `git archive` has no `--` before the ref to stop a
# value beginning with a dash being read as an option, and
# `--upload-pack=<program>` is an option that names a program to run. The rest
# of the set is what real branch and tag names are made of -- "main",
# "refs/heads/slfo-1.1", "SLFO-1.1.99-15.1". Nothing more is refused here: a ref
# that is well-formed and does not exist is the remote's answer to give, and
# step 3 of the fetch reports it.
#
# fullmatch, not match: "$" also matches just before a terminal newline, so
# re.match(r"[\w./-]+$", "main\n") succeeds and the allowlist leaks.
#
# The class is spelled out rather than written \w, which on a str pattern is
# unicode-aware and would admit "café" against a docstring and a message that
# both promise ASCII. Both sibling repositories spell theirs out for the same
# reason.
_VALID_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")


@runtime_checkable
class RemoteArchiveRepository(Protocol):
    """Fetch the SLFO maintainership document from its git remote."""

    def fetch_maintainership(self, ref: str) -> bytes:
        """Return the bytes of ``_maintainership.json`` as of ``ref``.

        Args:
            ref: Branch or tag to read the document at, e.g. ``"main"``. Must
                begin with ``[A-Za-z0-9_]`` and continue in ``[A-Za-z0-9_./-]``;
                it comes from the CLI's ``--ref`` and occupies an argv slot of
                its own, and nothing downstream re-validates it. What the remote
                agrees to serve for a given ref is the remote's rule, not this
                allowlist's, and it arrives as a non-zero exit.

        Returns:
            The document as raw bytes, read out of the tar stream in memory.
            Parsing it is the caller's job: this module is the transport, and
            the JSON shape has a module of its own.

        Raises:
            InputError: If ``ref`` leaves the allowlist. Raised before the value
                reaches a subprocess.
            MissingBinaryError: If ``git`` is not on ``PATH``.
            NetworkTimeoutError: If ``git`` outlives
                :data:`GIT_TIMEOUT_SECONDS`.
            DataSourceError: If ``git`` exits non-zero -- every non-zero exit,
                an unknown ref and an absent file included -- or what it printed
                is not a readable uncompressed tar, does not hold exactly one
                member, or holds one that is not a plain regular file -- tar
                type ``REGTYPE``, which is narrower than ``isreg()`` -- named
                ``_maintainership.json``.
            OSError: If ``git`` exists but cannot be executed. Left unwrapped for
                the caller, which owns the mapping from failure to exit code.
        """
        ...


class RemoteArchiveRepositoryImpl:
    """Adapter implementation backed by ``git archive --remote``."""

    def fetch_maintainership(self, ref: str) -> bytes:
        """Return the maintainership document at ``ref``, read from the remote.

        The contract -- arguments, return value and the exceptions raised -- is
        stated once, on :meth:`RemoteArchiveRepository.fetch_maintainership`.
        Restating it here would give the two halves room to drift.
        """
        # Before the ref becomes an argv slot, not after: a rejected ref must not
        # have been handed to a subprocess by the time it is refused.
        _validate_ref(ref)
        return _extract_document(_fetch_archive(ref))


def _validate_ref(ref: str) -> None:
    """Reject a ref that must not reach git's argv."""
    if _VALID_REF.fullmatch(ref):
        return
    # !r, not the bare value: it comes from the command line, and repr escapes
    # the control characters and ANSI sequences that would otherwise rewrite the
    # terminal line the complaint is read on.
    raise InputError(f"invalid git ref: {ref!r}; expected [A-Za-z0-9_] followed by [A-Za-z0-9_./-]")


def _fetch_archive(ref: str) -> bytes:
    """Return the tar stream ``git archive`` printed for the document at ``ref``."""
    try:
        proc = subprocess.run(
            # A list, never a shell string, and the pathspec sits after "--" so
            # no argument of this command can be read as another kind.
            [
                "git",
                "archive",
                "--remote=gitea@src.suse.de:products/SLFO.git",
                ref,
                "--",
                "_maintainership.json",
            ],
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
            # /dev/null plus GIT_TERMINAL_PROMPT=0 make an unauthenticated remote
            # fail fast instead of blocking on a credential prompt nobody is
            # watching for. Deliberately NOT `ssh -o BatchMode=yes`: BatchMode
            # also refuses passphrase prompts, which breaks a passphrase-
            # protected key that is not already loaded into an agent -- a working
            # setup this must not penalise.
            stdin=subprocess.DEVNULL,
            # GIT_ALLOW_PROTOCOL pinned to ssh alone, which is measured to be the
            # only transport that reaches this remote. The pin exists because it
            # is the only lever that holds: it overrides the operator's
            # gitconfig, an inherited GIT_ALLOW_PROTOCOL and an inherited
            # GIT_CONFIG_COUNT alike, where `-c protocol.ext.allow=never` loses
            # to the environment variable. It is also the only defence against a
            # `url.<x>.insteadOf` rewrite turning this URL into an `ext::` one
            # *inside* git, after any check on this side has passed -- and an
            # ext:: URL is command execution. os.environ is carried through
            # because ssh needs HOME, SSH_AUTH_SOCK and PATH to authenticate.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "ssh"},
        )
    # FileNotFoundError, not OSError: a git that is present but not executable
    # raises PermissionError, which is also an OSError, and telling that user to
    # install git would send them after the wrong problem. Every other OSError
    # escapes raw by design -- errno says more than a wrapper could add. One
    # check and not two, either: shutil.which before the call asks the same
    # question the exec answers, and the two can disagree.
    except FileNotFoundError as exc:
        raise MissingBinaryError("git") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetworkTimeoutError(f"git archive {ref!r}", GIT_TIMEOUT_SECONDS) from exc
    if proc.returncode != 0:
        # Every non-zero exit, an unknown ref and an absent file included. The
        # alternative is matching git's stderr for "no such ref" or "did not
        # match any files", and those messages are gettext-translated by the
        # *remote's* locale -- forcing LC_ALL here would not change them. The
        # sibling owner repository refuses the same guess for the same reason:
        # getting it wrong turns a transport failure into a confident lie about
        # the ref. stderr is reproduced with !r because it is remote-influenced
        # text on its way to a terminal.
        raise DataSourceError(
            f"git archive {ref!r} exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()!r}"
        )
    return proc.stdout


def _extract_document(archive: bytes) -> bytes:
    """Return the content of the one regular file the tar stream must hold.

    Read from memory and never written out: ``extractall`` is never called, so
    no member name and no member type can decide where a byte lands on disk.
    """
    try:
        # mode="r:", not "r": "r" transparently inflates gzip, bz2 and xz, and
        # `git archive` emits uncompressed tar. A compressed stream here is
        # therefore always wrong, and "r" would decompress it before anything
        # below could say so.
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            members = tar.getmembers()
            # The pathspec names one file in the repository root, so one member
            # is the only shape this command produces. An empty archive would
            # otherwise read as an empty document and a second member would be
            # silently ignored.
            if len(members) != 1:
                raise DataSourceError(
                    f"git archive answered with {len(members)} tar members where exactly 1 belongs"
                )
            member = members[0]
            # A symlink member named like the document reads whatever it points
            # at the moment anything follows it, and a directory member carries
            # no content at all.
            #
            # REGTYPE, not isreg(): REGULAR_TYPES also holds GNUTYPE_SPARSE,
            # CONTTYPE and AREGTYPE, and `git archive` emits none of them. Sparse
            # is the one that costs something -- its length is declared in a
            # header field instead of carried by the stream, so 1,536 bytes of
            # archive return a megabyte from read() and the field can ask for
            # gigabytes.
            if member.type != tarfile.REGTYPE:
                raise DataSourceError(
                    f"The tar member {member.name!r} is tar type {member.type!r}, expected a "
                    f"plain regular file ({tarfile.REGTYPE!r})"
                )
            # Being the only member does not make it the file the pathspec named,
            # and reading a different document as the maintainership file is a
            # complete, plausible, entirely wrong report rather than a failure.
            if member.name != "_maintainership.json":
                raise DataSourceError(
                    f"The tar member is {member.name!r}, expected '_maintainership.json'"
                )
            stream = tar.extractfile(member)
            # Narrowing for mypy, not a guard: extractfile is typed Optional, but
            # it returns None only for a member with no content -- a directory, a
            # chrdev -- and isreg() above already refused every one of those. An
            # `if stream is None: raise` here would be a branch no input can take.
            assert stream is not None
            with stream:
                return stream.read()
    # TarError is not an OSError -- verified -- so without an arm of its own it
    # escapes the taxonomy as a traceback rather than as a named failure.
    except tarfile.TarError as exc:
        raise DataSourceError(f"git archive did not answer with a readable tar: {exc}") from exc
