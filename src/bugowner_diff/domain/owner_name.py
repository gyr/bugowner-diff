"""The single definition of the ``group:`` owner-name tag rule.

Both sides of the diff hand out owners in two kinds: the OBS owner search
returns ``<person>`` and ``<group>`` elements, the SLFO maintainership document
holds ``users`` and ``groups`` lists. Equality is exact set equality on tagged
names, so if each side spelled the tag itself, drift between the two spellings
would emit a silently wrong ``none``. Every caller tags through this module.

The prefix itself is private, so tagging has exactly one spelling. A caller
pairs the two functions with the source lists it reads --
``for key, tag in (("users", tag_user), ("groups", tag_group))`` -- and never
concatenates the prefix; the concatenation nobody writes is the one that cannot
drift.
"""

_GROUP_PREFIX = "group:"


def tag_user(name: str) -> str:
    """Tag a user name for an owner set.

    Users carry no tag; the function exists so that a caller pairs it with
    :func:`tag_group` and never has to decide on its own what an untagged name
    looks like.

    Args:
        name: Raw user name, exactly as the source spelled it, already narrowed
            to ``str`` by the caller. Both sources are untrusted documents;
            nothing in this module validates.

    Returns:
        The name, unchanged. No stripping and no case folding: both sources are
        authoritative about their own spelling, and normalizing here would make
        two genuinely different owners compare equal.
    """
    return name


def tag_group(name: str) -> str:
    """Tag a group name for an owner set.

    Args:
        name: Raw group name, exactly as the source spelled it, already
            narrowed to ``str`` by the caller.

    Returns:
        The name prefixed with ``group:``, which is also what the CSV cell
        shows. Unnormalized, for the reason given in :func:`tag_user`.
    """
    return f"{_GROUP_PREFIX}{name}"


def ambiguity_reason(name: str, *, is_group: bool) -> str | None:
    """Classify how a raw owner name would render ambiguously once tagged.

    Detection lives beside the tag rule because two of the three classes are
    consequences of that rule. What the caller does with a reason -- warn,
    count, name the package it appeared in -- is the caller's business; nothing
    here logs, rewrites or drops a name.

    The three classes:

    - An empty name identifies nobody and contributes an invisible token.
    - A user named ``group:x`` tags to the same string as a group named ``x``.
      ``group:`` is not reserved on either side, so the two collapse into one
      set member and one of the owners is lost.
    - Whitespace inside a name splits it into what looks like several names,
      because the CSV cell joins an owner set with a space.

    Args:
        name: Raw owner name, **before** tagging, already narrowed to ``str``
            by the caller. An empty group name tags to the non-empty
            ``group:``, so testing the tagged string for emptiness would miss
            half of that class.
        is_group: True when the name came from the group-valued side. It
            suppresses the collapse test, which would otherwise match every
            group in the document: a group's own tag starts with ``group:`` by
            construction, and a group named ``group:x`` tags to
            ``group:group:x``, a string no other group can produce.

    Returns:
        A reason phrase, or None when the name renders unambiguously.

        The phrase is a sentence fragment completing the frame
        ``f"Owner name {tagged!r} of package {package!r} renders ambiguously:
        {reason}"``. Every caller writes that wrapper itself, so the frame is
        stated here to keep two call sites from spelling the same message two
        ways.

        At most one reason, and the order is deliberate. Emptiness is disjoint
        from the other two. The collapse outranks whitespace because a collapse
        is the only class that loses information -- two owners become one set
        member and no later stage can recover them -- whereas whitespace merely
        makes a cell hard to read, and the caller sees the offending name
        either way.
    """
    if not name:
        return "it comes from an empty name, so it identifies no owner"
    if not is_group and name.startswith(_GROUP_PREFIX):
        return f"a user name starting with {_GROUP_PREFIX!r} renders identically to a group"
    # Per character rather than `" " in name`, so a tab, a newline or a
    # no-break space is caught too.
    if any(character.isspace() for character in name):
        return "it holds whitespace, and owner cells join names with a space"
    return None
