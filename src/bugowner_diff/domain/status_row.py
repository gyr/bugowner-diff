"""Outcome vocabulary of the ownership diff and the row that carries it."""

from dataclasses import dataclass
from enum import StrEnum


class Status(StrEnum):
    """Outcome of comparing one package's owners across the two sources.

    Members are :class:`~enum.StrEnum` so that a member *is* the CSV cell the
    writer emits; a plain :class:`~enum.Enum` would render as ``Status.ADDED``.

    - ``ADDED`` -- absent from the IBS project listing.
    - ``REMOVED`` -- present there, absent from the maintainership file.
    - ``ADOPTED`` -- nobody owns it on 15, someone does on 16.
    - ``UNMAINTAINED`` -- owned on 15, but the maintainership file names nobody.
    - ``NONE`` -- both sides agree; no difference to report.
    - ``CHANGED`` -- both sides name owners, and the two sets differ.

    The members are declared in the order
    ``services.status_service.classify`` tests them, so the vocabulary reads as
    the ladder that produces it.
    """

    ADDED = "added"
    REMOVED = "removed"
    ADOPTED = "adopted"
    UNMAINTAINED = "unmaintained"
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
    sentinel for "the package is absent on that side": absent and unowned are
    facts :class:`Status` already keeps apart, on 15 by ``ADDED`` against
    ``ADOPTED`` and on 16 by ``REMOVED`` against ``UNMAINTAINED``, so a second
    encoding of the same fact could only ever contradict the first. Each pair
    names the distinction its members carry, not a definition of either one:
    ``REMOVED`` also takes an unowned 15 side, when 16 has no entry at all.

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
