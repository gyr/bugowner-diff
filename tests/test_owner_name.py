"""Tests for the single definition of the ``group:`` owner-name tag rule."""

import pytest

from bugowner_diff.domain.owner_name import ambiguity_reason, tag_group, tag_user


def test_tag_group_prefixes_the_group_name() -> None:
    assert tag_group("team-a") == "group:team-a"


def test_tag_user_returns_the_user_name_unchanged() -> None:
    assert tag_user("user-a") == "user-a"


@pytest.mark.parametrize("name", ["", " leading", "trailing ", "Mixed.Case_1+2-3"])
def test_tagging_never_normalizes_the_name_it_is_given(name: str) -> None:
    # Both sources are authoritative about their own spelling, and equality is
    # exact set equality: stripping or case-folding here would make two
    # genuinely different owners compare equal and emit a wrong `none`.
    assert tag_user(name) == name
    assert tag_group(name) == f"group:{name}"


def test_a_user_literally_named_group_x_collapses_onto_the_group_named_x() -> None:
    # `group:` is not a reserved prefix on either side, so the tag rule is not
    # injective. This is the information loss `ambiguity_reason` exists to flag.
    assert tag_user("group:team-a") == tag_group("team-a")


def test_ambiguity_reason_flags_the_user_whose_name_collapses_onto_a_group() -> None:
    reason = ambiguity_reason("group:team-a", is_group=False)

    assert reason is not None
    assert "group:" in reason


@pytest.mark.parametrize("name", ["team-a", "group:team-a"], ids=["plain", "prefixed"])
def test_ambiguity_reason_never_reports_a_collapse_against_a_group(name: str) -> None:
    # Every tagged group starts with `group:` by construction, so a rule reading
    # the tagged string would flag the whole document. A group named
    # `group:team-a` tags to `group:group:team-a`, which no other group can
    # produce -- only a user spelled that way collides, and that user is flagged
    # on its own side.
    assert ambiguity_reason(name, is_group=True) is None


@pytest.mark.parametrize("is_group", [False, True], ids=["user", "group"])
def test_ambiguity_reason_flags_an_empty_name(is_group: bool) -> None:
    # An empty group name tags to the non-empty "group:", so a caller testing
    # the tagged string for emptiness would miss half of this class.
    assert ambiguity_reason("", is_group=is_group) is not None


@pytest.mark.parametrize(
    "name",
    ["user a", "user\ta", "user\na", "user\va", "user\xa0a", " user-a", "user-a "],
    ids=["space", "tab", "newline", "vtab", "nbsp", "leading", "trailing"],
)
def test_ambiguity_reason_flags_whitespace_anywhere_in_a_name(name: str) -> None:
    # Per character, not `" " in name`: the CSV cell joins names with a space,
    # so any whitespace splits one name into what looks like several. The nbsp
    # case is written `\xa0` rather than as a literal, so that it stays visibly
    # distinct from the plain-space case and cannot be normalized away by an
    # editor into a silent duplicate of it.
    assert ambiguity_reason(name, is_group=False) is not None


@pytest.mark.parametrize(
    "name",
    ["user-a", "team-a", "a", "a.b_c+d-e", "ünïcode", "Group:team-a", "xgroup:team-a"],
)
def test_ambiguity_reason_passes_names_that_render_unambiguously(name: str) -> None:
    # `Group:` and `xgroup:` are near-misses: the collapse test is exact and
    # anchored, so neither is a false positive.
    assert ambiguity_reason(name, is_group=False) is None
    assert ambiguity_reason(name, is_group=True) is None


def test_the_collapse_outranks_whitespace_when_a_name_is_both() -> None:
    # A collapse loses information -- two owners become one set member and no
    # later stage can recover them -- while whitespace only makes a cell hard to
    # read. The offending name reaches the caller either way, so reporting the
    # collapse costs nothing and reporting the whitespace would hide it.
    reason = ambiguity_reason("group:team a", is_group=False)

    assert reason == ambiguity_reason("group:team-a", is_group=False)


def test_an_empty_name_reports_emptiness_rather_than_whitespace() -> None:
    # "" and " " are different defects and must not share one reason phrase.
    assert ambiguity_reason("", is_group=False) != ambiguity_reason(" ", is_group=False)
