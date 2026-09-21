"""Turn one package's two owner sets into the diff status they imply.

This is where the two sides of the run meet: the tagged owners
``obs_owner_repository.find_owners`` reports for the IBS project, and the tagged
maintainers ``maintainership_snapshot.parse_tagged_snapshot`` read out of the
SLFO document. Both arrive already tagged, so the comparison is exact set
equality on tagged names and nothing here normalises anything.
"""

from bugowner_diff.domain.status_row import Status, StatusRow


def classify(
    package: str,
    sle15_owners: frozenset[str] | None,
    slfo_maintainers: frozenset[str] | None,
) -> StatusRow:
    """Return the row this package's ownership on both sides adds up to.

    Precondition, relied on and deliberately not checked: ``None`` means the
    source has no entry for this package, and never that the source failed.
    Every source in this codebase raises -- ``DataSourceError``,
    ``NetworkTimeoutError``, ``MissingBinaryError``, ``InputError`` -- and none
    returns a sentinel, so a timeout or a corrupt document ends the run long
    before this function is reached. Re-checking that here would be a second
    copy of a guarantee the repositories already give.

    Args:
        package: Package name, as read from the input file.
        sle15_owners: Tagged owners from the IBS owner search, or ``None`` when
            the package is absent from the project listing and no owner search
            was made for it. An empty set is a different answer: the search ran
            and named nobody.
        slfo_maintainers: Tagged maintainers from the SLFO document, or ``None``
            when the document has no entry for the package. No measured entry
            has both its owner lists empty, so that document never answers with
            an empty set.

    Returns:
        The package's :class:`~bugowner_diff.domain.status_row.StatusRow`. A
        side that has no entry becomes an empty cell, since ``StatusRow`` has no
        ``None`` sentinel by design; a side that answered is carried through
        unchanged, including the 16 column of an ``added`` package, which usually
        does have SLFO maintainers to show.
    """
    # The order of the five tests is load-bearing. Both absence tests come
    # before both emptiness tests, matching the sibling project's ladder: a side
    # with no entry is a stronger fact than a side that answered and named
    # nobody, so it is settled first.
    #
    # Within the absences, `sle15_owners is None` comes first because three
    # packages in the measured 119-name run -- `hiredis`, `iansible-trento`,
    # `toolbox-branding-SLE` -- are absent from both sources, and a package
    # nobody has ever packaged is `added` rather than removed from a project it
    # was never in. The consequence on the other side is that an empty 15 side
    # no longer stops a package missing from the 16 document being `removed`;
    # that shape does not occur in the measured data, where the one removed
    # package has a 15-side owner, so the rung order alone decides it.
    if sle15_owners is None:
        status = Status.ADDED
    elif slfo_maintainers is None:
        status = Status.REMOVED
    elif not sle15_owners:
        status = Status.ADOPTED
    elif not slfo_maintainers:
        status = Status.UNMAINTAINED
    elif sle15_owners == slfo_maintainers:
        status = Status.NONE
    else:
        status = Status.CHANGED
    return StatusRow(
        package=package,
        sle15_owners=frozenset() if sle15_owners is None else sle15_owners,
        slfo_maintainers=frozenset() if slfo_maintainers is None else slfo_maintainers,
        status=status,
    )
