"""List the packages of an IBS project, inherited ones included.

This is the source that decides which packages are ``added``: a name absent from
the listing is a package the 15 side does not have. The listing is fetched with
``?expand=1``, so it carries packages inherited from the project's link as well
as the locally defined ones -- 606 entries without it, 62809 with, and 88 of
the 119 input packages exist only by inheritance. Nothing here filters them
out; that is the settled decision the ``added`` count depends on.

Transport is an ``osc api`` subprocess, never an HTTP client: ``api.suse.de``
wants an SSH-signature auth that HTTP Basic cannot supply, and delegating to
``osc`` reuses the credentials the user already has configured. The argv is a
list with the project embedded in the API path, so no quoting rule and no
argument-parsing rule is being relied on.

Everything the subprocess prints is untrusted: the body goes through a size cap,
a DTD-forbidding parser and a set of document-shape checks, and anything that
fails one of them stops the run with a message naming what was expected and what
arrived, rather than being worked around.
"""

import re
import subprocess
from typing import Protocol, runtime_checkable

# The type only, for annotations. Parsing goes through defusedxml below and
# never through this module: defusedxml builds stdlib Elements but re-exports no
# name for them, so there is nowhere safer to take the annotation from.
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from bugowner_diff.exceptions import (
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)

OBS_API_URL = "https://api.suse.de"

# Two minutes. The expanded listing is the largest document this tool fetches
# -- 62809 entries, 4,792,776 bytes on 2026-09-16 -- and it is served from a
# single unparallelised request, so the deadline has to cover a slow link
# rather than a typical one. Its job is only to make an osc that never returns
# a failure instead of a hang.
OSC_TIMEOUT_SECONDS = 120

# How many characters of osc's stderr a failure message reproduces -- applied
# to the decoded text, not to the bytes. A failing osc can print a full
# traceback, and the message reaches stderr and, once -v/-d arrive, a log file.
_MAX_SHOWN_STDERR_LENGTH = 500

# The root element of the document /source/<project> answers with, and the only
# element it may hold.
_LISTING_ROOT_TAG = "directory"
_LISTING_ENTRY_TAG = "entry"

# The attribute that carries the entry count the listing declares about itself.
_LISTING_COUNT_ATTRIBUTE = "count"

# How much of an element tag a failure message echoes. A tag is remote-supplied
# and has no length bound of its own: a 2 MB element name parses fine and would
# otherwise be reproduced whole.
_MAX_SHOWN_TAG_LENGTH = 80

# 32 MiB, against a real listing of 4,792,776 bytes measured on 2026-09-16 --
# roughly 6.8x headroom.
#
# What it bounds is the serialised body, not the tree parsed out of it. Measured
# amplification from body to in-memory tree is roughly 22x for a flat listing
# and 55x for a deeply nested one, so a body at this cap can still cost well
# over a gigabyte of heap. Bounding the tree would mean an incremental parse;
# for an authenticated internal source whose real answer is 4.8 MB, the body cap
# plus the CLI's catch-all is the trade taken.
#
# Known gap, accepted: subprocess.run(capture_output=True) buffers the whole of
# stdout in memory before this cap can be consulted, so a hostile
# multi-gigabyte body raises MemoryError rather than being refused here. Closing
# it would mean managing the pipe by hand for a source that is authenticated,
# internal and already trusted to the extent of being parsed; the CLI's
# catch-all covers the MemoryError.
MAX_LISTING_BYTES = 32 * 1024 * 1024

# Real IBS project names are well under 100 characters; the bound caps the
# growth of the API path built from the name.
MAX_PROJECT_NAME_LENGTH = 200

# How much of a rejected project name the error message echoes, on the same
# reasoning as the package-list repository's own limit: the message reaches
# stderr and, once -v/-d arrive, a log file.
_MAX_SHOWN_PROJECT_LENGTH = 80

# The sibling project's allowlist, built from the length constant so the two
# cannot drift, and tightened in one place: the first character must be
# alphanumeric. The character set admits every character a real project name
# uses -- "SUSE:SLE-15-SP7:GA", "SUSE:ALP:Source:Standard:1.0" -- and excludes
# the ones that would change the request: "?" and "&" append parameters to the
# API call, "/" walks out of /source/, "#" truncates the path, and whitespace or
# control characters have no business in an argv.
#
# The set alone is not enough, because "." and "-" are in it and a name built
# only from those is not inert. osc hands its URL to urllib3's parse_url, which
# normalises dot segments away client-side, before a byte leaves the machine: a
# project of "." turns /source/.?expand=1 into the project *index* at /source/,
# and ".." escapes /source/ altogether. Both answer with a well-formed document
# that is the listing of no project, and this is the only place positioned to
# refuse them -- by the time the request is built the evidence is gone. The
# leading-alphanumeric rule costs nothing: every real project name starts with a
# letter, and it also makes a leading-dash name unreachable rather than merely
# neutralised by its placement in the argv.
_VALID_PROJECT_NAME = re.compile(rf"[A-Za-z0-9][A-Za-z0-9:_.+-]{{0,{MAX_PROJECT_NAME_LENGTH - 1}}}")


@runtime_checkable
class ObsProjectRepository(Protocol):
    """Report which packages an OBS project offers."""

    def list_packages(self, project: str) -> frozenset[str]:
        """Return every package name the project offers, inherited ones included.

        Args:
            project: OBS project name, e.g. ``"SUSE:SLE-15-SP7:GA"``. Must begin
                with ``[A-Za-z0-9]``, continue in ``[A-Za-z0-9:_.+-]`` and stay
                within :data:`MAX_PROJECT_NAME_LENGTH` characters; it reaches an
                API path, and nothing downstream re-validates it.

        Returns:
            The package names, as a set: the report asks only whether a package
            is present, and the listing carries no order worth preserving. The
            expanded listing deliberately includes packages inherited from the
            linked project -- 88 of the 119 input packages exist only by
            inheritance -- so nothing here filters them out. An empty set means
            an empty project, and is not an error.

        Raises:
            InputError: If ``project`` leaves the allowlist. Raised before the
                name reaches a subprocess.
            MissingBinaryError: If ``osc`` is not on ``PATH``.
            NetworkTimeoutError: If ``osc`` outlives
                :data:`OSC_TIMEOUT_SECONDS`.
            DataSourceError: If ``osc`` exits non-zero, or the body it printed
                is over :data:`MAX_LISTING_BYTES`, carries a DOCTYPE, is not
                well-formed XML, is not rooted at ``<directory>``, holds an
                element other than ``<entry>`` or an ``<entry>`` that has
                children, holds an ``<entry>`` whose name is missing or empty,
                or declares a ``count`` other than the number of entries it
                holds.
            OSError: If ``osc`` exists but cannot be executed. Left unwrapped for
                the caller, which owns the mapping from failure to exit code.
        """
        ...


class ObsProjectRepositoryImpl:
    """Adapter implementation backed by ``osc api``."""

    def list_packages(self, project: str) -> frozenset[str]:
        """Return the packages of ``project``, read from ``osc``.

        The contract -- arguments, return value and the exceptions raised -- is
        stated once, on :meth:`ObsProjectRepository.list_packages`. Restating it
        here would give the two halves room to drift.
        """
        # Before the name becomes an API path, not after: a rejected project
        # must not have been handed to a subprocess by the time it is refused.
        _validate_project(project)
        return _parse_listing(_fetch_listing(project))


def _validate_project(project: str) -> None:
    """Reject a project name that must not reach an API path."""
    # fullmatch, not match: the sibling project's two copies of this check drifted
    # into re.match and re.fullmatch, and re.match would accept anything at all
    # provided it merely *starts* with an allowed run of characters.
    if _VALID_PROJECT_NAME.fullmatch(project):
        return
    shown = project[:_MAX_SHOWN_PROJECT_LENGTH]
    if len(project) > _MAX_SHOWN_PROJECT_LENGTH:
        shown += "..."
    # !r, not the bare name: the value comes from the command line, and repr
    # escapes the control characters and ANSI sequences that would otherwise
    # rewrite the terminal line the complaint is read on.
    raise InputError(
        f"invalid project name: {shown!r}; expected [A-Za-z0-9] followed by "
        f"[A-Za-z0-9:_.+-], up to {MAX_PROJECT_NAME_LENGTH} characters"
    )


def _parse_listing(body: bytes) -> frozenset[str]:
    """Return the entry names of a ``<directory>`` document."""
    # Checked before the parser is handed the body, not after: the point is to
    # refuse the allocation of a tree over an oversized document.
    if len(body) > MAX_LISTING_BYTES:
        raise DataSourceError(
            f"The project listing is {len(body)} bytes, over the {MAX_LISTING_BYTES} byte "
            f"limit; refusing to parse it"
        )
    try:
        root = ET.fromstring(body, forbid_dtd=True)
    # DefusedXmlException, not ValueError: the two are related by inheritance
    # but a bare ValueError here would also swallow whatever the expat layer
    # reports for an oversized numeric attribute, and report it as a DOCTYPE.
    except DefusedXmlException as exc:
        raise DataSourceError(
            "The project listing carries a DOCTYPE declaration and was not parsed; "
            "a legitimate OBS listing never does. Refused to keep an entity expansion "
            "from being evaluated."
        ) from exc
    # ParseError is a SyntaxError, not a ValueError, so it is not covered by the
    # arm above however that arm is widened. It is also what a reference to an
    # undeclared entity arrives as once the DTD is forbidden.
    except ET.ParseError as exc:
        raise DataSourceError(f"The project listing is not well-formed XML: {exc}") from exc

    # OBS answers some failures with a well-formed <status> document, and osc
    # does not reliably exit non-zero when it does. Reading entries out of one
    # of those finds nothing, and an empty listing classifies every package as
    # `added` -- a complete, plausible, entirely wrong report. The root element is
    # what tells the two documents apart.
    if root.tag != _LISTING_ROOT_TAG:
        raise DataSourceError(
            f"Expected a <{_LISTING_ROOT_TAG}> project listing but the root element is "
            f"{_shown_tag(root.tag)}; refusing to read package names out of a different document"
        )

    names: set[str] = set()
    entries = 0
    # Walking the children and refusing what does not belong, rather than
    # root.findall("entry") or root.iter("entry"). Both of those *search*, and
    # whatever a search fails to match is silently absent from the result --
    # which here means a package reported as absent from a project it is in, the
    # one failure this module must not have. Two documents show it, verified:
    # <entry><entry name='spice'/></entry>, where findall sees only the outer,
    # nameless element and the nested name is gone -- iter is recursive and does
    # find both, so nesting defeats findall alone -- and
    # <entry xmlns='urn:x' name='spice'/>, which ElementTree renders as
    # "{urn:x}entry" so neither call matches it at all. Iterating cannot drop
    # anything: every child is either counted or refused. Comments, processing
    # instructions and whitespace never become children, so document formatting
    # cannot trip the rule.
    #
    # Fail-closed, and the cliff is deliberate: the *package* listing at
    # /source/<prj>/<pkg> shares this root and carries <linkinfo> and
    # <serviceinfo> siblings, so an OBS that began adding siblings to the
    # *project* listing would stop this tool rather than degrade it. The message
    # names the tag it did not expect, which makes that a one-read diagnosis --
    # the trade taken over reading names out of a document of unknown shape.
    for child in root:
        if child.tag != _LISTING_ENTRY_TAG:
            raise DataSourceError(
                f"The project listing holds a {_shown_tag(child.tag)} element where only "
                f"<{_LISTING_ENTRY_TAG}> belongs; refusing to read package names out of a "
                f"document whose shape is not the one OBS answers with"
            )
        if len(child):
            raise DataSourceError(
                f"The project listing holds an <{_LISTING_ENTRY_TAG}> with child elements, "
                f"which the directory schema has none of; a name nested inside another entry "
                f"is a package this would otherwise report as absent from a project it is in"
            )
        entries += 1
        name = child.get("name")
        # Refused, not skipped. Skipping is commit 7's rule for an <owner>
        # without a package attribute, but there the missing attribute marks a
        # different kind of element -- project-level fallback -- that is meant
        # to be ignored. The directory schema has no such element: an entry
        # without a name is a package whose name was lost, and dropping it
        # reports that package as absent from a project it is in. `not name`
        # rather than `is None`, because name="" is present but is not a package
        # name either, and it would join the set and match nothing.
        if not name:
            raise DataSourceError(
                f"The project listing holds an <{_LISTING_ENTRY_TAG}> whose name attribute is "
                f"missing or empty; refusing to answer from a listing that is missing package "
                f"names"
            )
        names.add(name)

    _check_declared_count(root, entries)
    # An empty listing is returned as such: the root element already proved the
    # document is a listing, and an empty project is a legitimate answer.
    return frozenset(names)


def _check_declared_count(root: Element, entries: int) -> None:
    """Cross-check the entry count a listing declares against what was read.

    A second opinion on the shape checks in :func:`_parse_listing` rather than a
    substitute for them: the document states its own entry count, so an element
    that never reached the loop can be caught without knowing how it went
    missing.

    Counted as elements rather than as distinct names. A repeated name collapses
    into the set, which is correct -- membership is all the report asks of it --
    and must not be mistaken for an entry that was lost. The real listings carry
    no duplicates anyway, both probe captures declaring exactly what they hold.

    The attribute is remote-supplied, so it is trusted only when it is plain
    ASCII digits: ``int`` also accepts surrounding whitespace, PEP 515
    underscores and every Unicode decimal digit, and none of those should get to
    decide whether a run fails. Anything else, including an absent attribute, is
    treated as no declaration at all -- declining to hold a second opinion is the
    safe way to be unsure about one.
    """
    declared = root.get(_LISTING_COUNT_ATTRIBUTE)
    if declared is None or not (declared.isascii() and declared.isdigit()):
        return
    if int(declared) != entries:
        raise DataSourceError(
            f"The project listing declares {_LISTING_COUNT_ATTRIBUTE}={declared} but holds "
            f"{entries} entries; refusing to answer from a listing that does not hold what "
            f"it says it does"
        )


def _fetch_listing(project: str) -> bytes:
    """Return the body ``osc`` printed for the expanded listing of ``project``."""
    api_path = f"/source/{project}?expand=1"
    try:
        proc = subprocess.run(
            # A list, never a shell string, and the project is embedded in the
            # API path rather than passed as an argument of its own: a project
            # whose name begins with a dash can then never be read as an option
            # by osc.
            ["osc", "-A", OBS_API_URL, "api", api_path],
            capture_output=True,
            check=False,
            timeout=OSC_TIMEOUT_SECONDS,
            # osc prompts on the terminal for a password when its credentials
            # are missing or expired. /dev/null turns that prompt into an
            # immediate EOF rather than a wait nobody is watching for.
            stdin=subprocess.DEVNULL,
        )
    # FileNotFoundError, not OSError: an osc that is present but not executable
    # raises PermissionError, which is also an OSError, and telling that user to
    # install osc would send them after the wrong problem. Every other OSError
    # escapes raw by design -- errno says more than a wrapper could add.
    except FileNotFoundError as exc:
        raise MissingBinaryError("osc") from exc
    except subprocess.TimeoutExpired as exc:
        raise NetworkTimeoutError(f"osc api {api_path!r}", OSC_TIMEOUT_SECONDS) from exc
    if proc.returncode != 0:
        # Not RuntimeError, the sibling project's choice: RecursionError is a
        # RuntimeError subclass, so a caller catching one would also swallow a
        # crash in the parser and report it as a bad remote document.
        raise DataSourceError(
            f"osc api {api_path!r} exited {proc.returncode}: {_describe(proc.stderr)}"
        )
    return proc.stdout


def _shown_tag(tag: str) -> str:
    """Render an element tag for a failure message, bounded and escaped.

    Bounded because a tag is remote-supplied and has no length limit of its own:
    a 2 MB element name parses without complaint -- verified -- and echoing it
    whole turns a one-line complaint into a 2 MB one on stderr and, once
    ``-v``/``-d`` arrive, in a log file.

    Escaped because a namespaced tag carries its namespace URI, and a URI is an
    attribute value rather than an XML Name, so the Name production constrains
    nothing about it. expat does refuse a raw ESC there, and refuses it written
    as ``&#27;`` too, but U+202E survives into the tag and so does a newline
    written as ``&#10;`` -- verified. Left unescaped the first reverses the
    rendering of the rest of the line and the second breaks the message across
    lines. ``repr`` is the same answer :func:`_describe` gives for osc's stderr.
    """
    if len(tag) > _MAX_SHOWN_TAG_LENGTH:
        tag = tag[:_MAX_SHOWN_TAG_LENGTH] + "..."
    return repr(tag)


def _describe(stderr: bytes) -> str:
    """Render what ``osc`` printed on stderr, bounded and escaped.

    Bounded because a failing osc can print a traceback, and the whole of it
    would reach stderr and, once ``-v``/``-d`` land, a log file. ``!r`` because
    the text is remote-influenced: repr escapes the control characters and ANSI
    sequences that would otherwise rewrite the terminal line the user reads the
    failure on.
    """
    text = stderr.decode("utf-8", "replace").strip()
    if len(text) > _MAX_SHOWN_STDERR_LENGTH:
        text = text[:_MAX_SHOWN_STDERR_LENGTH] + "..."
    return repr(text)
