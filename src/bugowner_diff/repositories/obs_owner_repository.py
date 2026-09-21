"""Resolve who owns a package, from the OBS owner search.

This is the 15 side of the diff: the owner set read here is compared, as exact
set equality on tagged names, against the one the SLFO maintainership document
gives. A package the search returns no owner for is ``adopted`` when that
document has an entry for it and ``removed`` when it does not: the 15 side alone
does not decide the status.

The query is ``/search/owner?package=<name>`` and carries neither a project nor
a ``filter``. Both omissions are deliberate and were measured: scoping the
search to a project answers with project-level fallback owners instead of the
package's own, ``filter=maintainer`` degrades the same way, and no filter at all
is equivalent to ``filter=bugowner,maintainer``. Every ``<person>`` and
``<group>`` the answer holds is an owner -- the ``project``, ``package`` and
``role`` attributes are read by nothing here, because the endpoint asked about
one package and is the source of truth for the ownership of it.

The package name is not validated here. ``package_list_repository`` is the one
chokepoint that checks it against ``[A-Za-z0-9._+-]``, and nothing downstream
re-checks a value that module already accepted.

Transport is an ``osc api`` subprocess for the reason the project-listing
repository gives -- ``api.suse.de`` wants an SSH-signature auth that HTTP Basic
cannot supply -- and the plumbing is duplicated from it rather than shared: two
call sites are not enough to justify an abstraction that would then own both.
"""

import logging
import subprocess
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

from bugowner_diff.domain.owner_name import ambiguity_reason, tag_group, tag_user
from bugowner_diff.exceptions import (
    DataSourceError,
    MissingBinaryError,
    NetworkTimeoutError,
)
from bugowner_diff.repositories.obs_project_repository import OBS_API_URL

logger = logging.getLogger(__name__)

# Two minutes. The answer is a handful of elements, so this is not a deadline
# scaled to the document; its job is to make an osc that never returns fail
# instead of hang. The sibling repository's identical value is justified by a
# 4.8 MB listing, which is a different argument, so this module keeps its own.
OSC_TIMEOUT_SECONDS = 120


@runtime_checkable
class ObsOwnerRepository(Protocol):
    """Report who owns a package, according to OBS."""

    def find_owners(self, package: str) -> frozenset[str]:
        """Return every owner the OBS search records for ``package``.

        A name that renders ambiguously is reported on this module's logger at
        WARNING and returned unchanged; the three classes are
        :func:`~bugowner_diff.domain.owner_name.ambiguity_reason`'s, and nothing
        here drops or rewrites a name OBS is authoritative about.

        Args:
            package: Package name, e.g. ``"vim"``. Validated once, where the
                package list is read; nothing here re-checks it. It is
                percent-encoded into the API path and never occupies an argv
                slot of its own.

        Returns:
            The owners, as a set of tagged names: a user contributes its bare
            name and a group the ``group:``-prefixed form, tagged through
            :mod:`bugowner_diff.domain.owner_name` so the two sides of the diff
            cannot spell the prefix differently. Every ``<person>`` and
            ``<group>`` of every ``<owner>`` element is included, with no
            filtering by role. An empty set means the search knows no owner for
            the package -- the answer for 3 of the 88 packages probed -- and is
            not an error.

        Raises:
            MissingBinaryError: If ``osc`` is not on ``PATH``.
            NetworkTimeoutError: If ``osc`` outlives
                :data:`OSC_TIMEOUT_SECONDS`.
            DataSourceError: If ``osc`` exits non-zero -- every non-zero exit,
                a 404 included -- or the body it printed carries a DOCTYPE, is
                not well-formed XML, is not rooted at ``<collection>``, holds an
                element other than ``<owner>``, holds an ``<owner>`` with a
                child other than ``<person>`` or ``<group>``, or holds a member
                that has child elements or whose name is missing or empty.
            OSError: If ``osc`` exists but cannot be executed. Left unwrapped for
                the caller, which owns the mapping from failure to exit code.
        """
        ...


class ObsOwnerRepositoryImpl:
    """Adapter implementation backed by ``osc api``."""

    def find_owners(self, package: str) -> frozenset[str]:
        """Return the owners of ``package``, read from ``osc``.

        The contract -- arguments, return value and the exceptions raised -- is
        stated once, on :meth:`ObsOwnerRepository.find_owners`. Restating it
        here would give the two halves room to drift.
        """
        return _parse_owners(_fetch_owners(package), package)


def _parse_owners(body: bytes, package: str) -> frozenset[str]:
    """Return the tagged owner names of a ``<collection>`` document.

    ``package`` is the package the document answers for, and is here only to
    locate the ambiguity warning: across a run that makes one lookup per
    package, a warning naming the owner but not the package says nothing about
    where it came from.
    """
    try:
        root = ET.fromstring(body, forbid_dtd=True)
    # DefusedXmlException, not ValueError: the two are related by inheritance,
    # but a bare ValueError here would also swallow whatever the expat layer
    # reports for an oversized numeric attribute and blame it on a DOCTYPE.
    except DefusedXmlException as exc:
        raise DataSourceError(
            "The owner search answered with a document carrying a DOCTYPE declaration and it "
            "was not parsed; a legitimate OBS answer never does. Refused to keep an entity "
            "expansion from being evaluated."
        ) from exc
    # ParseError is a SyntaxError, not a ValueError, so it is not covered by the
    # arm above however that arm is widened. It is also what a reference to an
    # undeclared entity arrives as once the DTD is forbidden.
    except ET.ParseError as exc:
        raise DataSourceError(f"The owner search answer is not well-formed XML: {exc}") from exc

    # OBS answers some failures with a well-formed <status> document, and osc
    # does not reliably exit non-zero when it does. Reading members out of one
    # finds nothing, and no owners is a plausible, complete and entirely wrong
    # `adopted` row -- or `removed`, when the 16 document has no entry either.
    # The root element is what tells the two documents apart.
    if root.tag != "collection":
        raise DataSourceError(
            f"Expected a <collection> owner search answer but the root element is "
            f"{root.tag!r}; refusing to read owners out of a different document"
        )

    names: set[str] = set()
    # Walking the children and refusing what does not belong, rather than
    # root.findall("owner") or root.iter("person"). Both of those *search*, and
    # whatever a search fails to match is silently absent from the result --
    # which here means an owner lost, printed as a difference against a package
    # that has none. Two documents show it, verified:
    # <person name='user-a'><person name='user-b'/></person>, where findall sees
    # only the outer element and the nested name is gone -- iter is recursive and
    # does find both, so nesting defeats findall alone -- and
    # <person xmlns='urn:x' name='user-a'/>, which ElementTree renders as
    # "{urn:x}person" so neither call matches it at all. Iterating cannot drop
    # anything: every child is either read or refused.
    for owner in root:
        if owner.tag != "owner":
            raise DataSourceError(
                f"The owner search answer holds a {owner.tag!r} element where only <owner> "
                f"belongs; refusing to read owners out of a document whose shape is not the "
                f"one OBS answers with"
            )
        for member in owner:
            if member.tag not in ("person", "group"):
                raise DataSourceError(
                    f"An <owner> element holds a {member.tag!r} element where only <person> "
                    f"and <group> belong; refusing to read owners out of a document whose "
                    f"shape is not the one OBS answers with"
                )
            # Before the name is read, not after: the outer element carries a
            # perfectly plausible name of its own, so reading it first would
            # mask the nested one rather than report it.
            if len(member):
                raise DataSourceError(
                    f"A <{member.tag}> element has child elements, which the owner search "
                    f"schema has none of; a name nested inside another member is an owner "
                    f"this would otherwise not report"
                )
            name = member.get("name")
            # `not name` rather than `is None`: name='' is present and is not a
            # name either, and it would join the set as a member matching
            # nobody. Refused rather than skipped -- an owner whose name was
            # lost understates the ownership of the package just as much.
            if not name:
                raise DataSourceError(
                    f"A <{member.tag}> element has a missing or empty name attribute; refusing "
                    f"to answer from a document that is missing owner names"
                )
            is_group = member.tag == "group"
            tagged = tag_group(name) if is_group else tag_user(name)
            # Reported and kept, never dropped or rewritten: OBS is
            # authoritative about the spelling of its own owners, and the
            # warning is what explains a cell that reads oddly later.
            reason = ambiguity_reason(name, is_group=is_group)
            if reason is not None:
                logger.warning(
                    f"Owner name {tagged!r} of package {package!r} renders ambiguously: {reason}"
                )
            names.add(tagged)
    # A bare <collection/> arrives here as an empty set, which is the real answer
    # for 3 of the 88 packages probed and is what later produces `adopted`, or
    # `removed` for a package the 16 document has no entry for.
    return frozenset(names)


def _fetch_owners(package: str) -> bytes:
    """Return the body ``osc`` printed for the owner search on ``package``.

    ``quote`` is what makes the name usable as a query value: ``+`` means a
    space in a query string and is not in quote's default safe set, so ``gtk+``
    is encoded as ``gtk%2B`` and the search asks about the package that was
    meant. ``safe=""`` changes exactly one character, ``/``, which the
    package-name allowlist forbids anyway -- defence in depth should that
    allowlist ever widen, and nothing to do with the ``+``.
    """
    api_path = f"/search/owner?package={quote(package, safe='')}"
    try:
        proc = subprocess.run(
            # A list, never a shell string, and the package is embedded in the
            # API path rather than passed as an argument of its own: a name
            # beginning with a dash can then never be read as an option by osc.
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
        # Every non-zero exit, a 404 for an unknown package included: the only
        # alternative is matching osc's English stderr, which changes with its
        # locale and version, and getting that wrong writes `adopted`, or
        # `removed`, for a package whose ownership was never read. stderr is
        # reproduced with !r
        # because it is remote-influenced text on its way to a terminal.
        raise DataSourceError(
            f"osc api {api_path!r} exited {proc.returncode}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()!r}"
        )
    return proc.stdout
