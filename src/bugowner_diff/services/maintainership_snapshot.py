"""Parse the SLFO maintainership document into tagged owner sets.

The bytes come from ``remote_archive_repository.fetch_maintainership``; this
module is where they stop being a document and become the SLFO side of the
diff. The measured shape, 2981 packages and ~295 KB, is::

    {"header": {...}, "project": {...},
     "packages": {"ethtool": {"groups": ["team-a"], "users": []}}}

``header`` and ``project`` are read by nothing here.
"""

import json
import logging
from typing import Any

from bugowner_diff.domain.owner_name import ambiguity_reason, tag_group, tag_user
from bugowner_diff.exceptions import DataSourceError

logger = logging.getLogger(__name__)


def _reject_repeated_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object, refusing the repeated key json.loads would swallow.

    Without this, ``json.loads`` keeps the last value for a repeated key and
    says nothing, which for a repeated package is a lost owner set and a diff
    answered from half the document.

    It runs on every object in the document, ``header`` and ``project``
    included; that is the hook's nature and not a reason to scope it, since a
    document that repeats a key anywhere is not the document this parses.

    ``DataSourceError`` derives from ``Exception`` and not from ``ValueError``,
    so this raise passes straight out through the ``(ValueError,
    RecursionError)`` arm around ``json.loads`` instead of being caught by it
    and re-reported as unreadable JSON.
    """
    built: dict[str, Any] = {}
    for key, value in pairs:
        if key in built:
            raise DataSourceError(
                f"The maintainership document repeats the key {key!r} in one object; "
                f"refusing to answer from a document whose later value silently wins"
            )
        built[key] = value
    return built


def _tagged_owners(package_name: str, entry: dict[str, Any]) -> frozenset[str]:
    """Return the tagged owner set of one package entry.

    ``package_name`` is carried only so that a complaint can name the entry it
    came from: nothing else finds one line in a 2981-package document.
    """
    names: set[str] = set()
    for key, tag in (("users", tag_user), ("groups", tag_group)):
        is_group = key == "groups"
        # The two keys are not equally optional, and the asymmetry is measured
        # rather than assumed: across the 2981 entries, `users` is present in
        # every one while `groups` is absent from 3 of them (`kernel-source`
        # among them). An absent `groups` is therefore the document saying the
        # package has no group owner, and reading it as anything else aborts
        # every run -- this loop reaches all 2981 entries whatever the input
        # list asks for. An absent `users` has never occurred, so defaulting it
        # too would absorb an unobserved case and understate who owns the
        # package; it keeps ending the run.
        owners = entry.get(key, []) if is_group else entry.get(key)
        # A present-but-wrong value is still a shape violation on either key. A
        # str passes `for name in ...` and would contribute one member per
        # character, which is why the type is checked rather than iterability.
        if not isinstance(owners, list):
            raise DataSourceError(
                f"The maintainership entry for package {package_name!r} has no usable "
                f"{key!r} list: expected a JSON array of names, got {type(owners).__name__}"
            )
        for name in owners:
            # Required by the contract this calls into, not defensive: tag_user,
            # tag_group and ambiguity_reason each document "already narrowed to
            # str by the caller", and this is that caller. It is also where the
            # Any that json.loads returns by nature stops.
            if not isinstance(name, str):
                raise DataSourceError(
                    f"The {key!r} list of package {package_name!r} holds {name!r}, "
                    f"expected owner names as JSON strings"
                )
            tagged = tag(name)
            # Reported and kept, never dropped, stripped or rewritten: the
            # document is authoritative about the spelling of its own owners,
            # and an empty name inside a list is the source's answer rather than
            # a violation of its shape. The message frame is fixed by
            # ambiguity_reason's docstring and shared with the OBS side, so the
            # two call sites cannot spell one message two ways.
            reason = ambiguity_reason(name, is_group=is_group)
            if reason is not None:
                logger.warning(f"Owner name {tagged!r} renders ambiguously: {reason}")
            names.add(tagged)
    return frozenset(names)


def parse_tagged_snapshot(document: bytes) -> dict[str, frozenset[str]]:
    """Return the owner set each package has in the maintainership document.

    Args:
        document: The document exactly as the git remote answered with it.
            ``json.loads`` takes bytes and does the UTF-8 decoding itself.

    Returns:
        A mapping from package name to its owners, tagged through
        :mod:`bugowner_diff.domain.owner_name` so that the two sides of the diff
        compare as exact set equality. An empty list is common -- 1655 of the
        2981 measured entries have no users and 1323 no groups -- but no entry
        has both empty, so this document on its own never answers with an empty
        owner set. It says who owns a package, never that nobody does.

        A name that renders ambiguously is reported on this module's logger at
        WARNING and kept unchanged, exactly as the OBS side does: an empty name
        in a ``users`` list is the source's answer about who owns the package,
        not a violation of the document's shape.

    Raises:
        DataSourceError: If the bytes are not readable JSON, repeat a key in any
            object, are not a JSON object holding a ``packages`` object, or hold
            an entry that is not an object carrying a ``users`` list of strings
            and, where it has one, a ``groups`` list of strings. The only type
            this module raises.
    """
    try:
        parsed = json.loads(document, object_pairs_hook=_reject_repeated_keys)
    # The pair is exhaustive over what a corrupt document can raise here, and it
    # is not the intuitive reading: json.JSONDecodeError is a ValueError, the
    # UnicodeDecodeError json.loads raises while decoding the bytes itself is
    # also a ValueError, and RecursionError -- what a deeply nested document
    # costs -- is a RuntimeError, which is why the second member exists.
    except (ValueError, RecursionError) as exc:
        # !r on the exception as well as on every remote value below: what the
        # decoder quotes back is remote-influenced text on its way to a
        # terminal, and repr is what escapes the control characters and bidi
        # marks that would otherwise rewrite the line the complaint is read on.
        # The sibling repository quotes git's stderr for the same reason.
        raise DataSourceError(f"The maintainership document is not readable JSON: {exc!r}") from exc
    # Readable JSON is not yet the document: subscripting a list or a string
    # below would raise a bare TypeError that names nothing and escapes the
    # taxonomy as a bug traceback.
    if not isinstance(parsed, dict):
        raise DataSourceError(
            f"The maintainership document is a {type(parsed).__name__}, "
            f"expected a JSON object at its root"
        )
    packages = parsed.get("packages")
    # Absent and present-but-wrong are one condition with one message: either
    # way the mapping this reads is not there, and an absent key read as an
    # empty one would answer that every package is missing from the document,
    # reporting the whole project as `dropped`.
    if not isinstance(packages, dict):
        raise DataSourceError(
            f"The maintainership document has no usable 'packages' object: expected a JSON "
            f"object of package entries, got {type(packages).__name__}"
        )
    snapshot: dict[str, frozenset[str]] = {}
    # package_name needs no isinstance check: a JSON object key is always a str.
    for package_name, entry in packages.items():
        if not isinstance(entry, dict):
            raise DataSourceError(
                f"The maintainership entry for package {package_name!r} is a "
                f"{type(entry).__name__}, expected a JSON object of owner lists"
            )
        snapshot[package_name] = _tagged_owners(package_name, entry)
    return snapshot
