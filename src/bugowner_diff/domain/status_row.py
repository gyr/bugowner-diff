"""Outcome vocabulary of the ownership diff and the row that carries it."""

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    """Outcome of comparing one package's owners across the two sources.

    Members are :class:`~enum.StrEnum` so that a member *is* the CSV cell the
    writer emits; a plain :class:`~enum.Enum` would render as ``Status.ADDED``.

    - ``ADDED`` -- absent from the IBS project listing.
    - ``UNMAINTAINED`` -- present there, but the owner search returns nobody.
    - ``REMOVED`` -- owned on the 15 side, absent from the maintainership file.
    - ``NONE`` -- both sides agree; no difference to report.
    - ``CHANGED`` -- both sides name owners, and the two sets differ.
    """

    ADDED = "added"
    UNMAINTAINED = "unmaintained"
    REMOVED = "removed"
    NONE = "none"
    CHANGED = "changed"


@dataclass(frozen=True)
class StatusRow:
    """One package's ownership on both sides, with the status that follows.

    Field order is the CSV column order, which the header spells
    ``package,15,16,change``: the fields are named for what they hold, the
    columns for how the report reads, so the ``status`` field is written under
    the ``change`` column.

    Both owner sets hold names already tagged by
    :mod:`bugowner_diff.domain.owner_name`, because equality is exact set
    equality on tagged names: an untagged name reaching this row would compare
    unequal to its tagged twin and report a spurious ``CHANGED``.

    An empty set means the side named nobody. There is deliberately no ``None``
    sentinel for "the package is absent on that side": :class:`Status` already
    separates absent-from-15 (``ADDED``) from present-but-unowned
    (``UNMAINTAINED``) and absent-from-16 (``REMOVED``) from agreed-empty, so a
    second encoding of the same fact could only ever contradict the first.

    Whether ``status`` actually follows from the two sets is
    ``services.status_service.classify``'s invariant, not this type's. A
    ``__post_init__`` re-checking it would be a second copy of the
    classification rules, which architect Q3 keeps in one pure function.

    Attributes:
        package: Package name, as read from the input file.
        sle15_owners: Tagged owners from the IBS owner search. Named for the
            default project ``SUSE:SLE-15-SP7:GA``; the column holds whichever
            project ``--project`` selected.
        slfo_maintainers: Tagged maintainers from the SLFO ``_maintainership.json``.
        status: Outcome of comparing the two sets.
    """

    package: str
    sle15_owners: frozenset[str]
    slfo_maintainers: frozenset[str]
    status: Status
