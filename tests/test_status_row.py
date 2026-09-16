"""Tests for the ownership-diff status vocabulary and its row value object."""

import dataclasses

import pytest

from bugowner_diff.domain.status_row import Status, StatusRow


def test_status_defines_exactly_the_documented_name_to_value_mapping() -> None:
    # The values are the literal CSV cells sampled in plan section 2.6, so the
    # writer emits the member itself instead of keeping a second lookup table
    # that could drift away from this one.
    assert {member.name: member.value for member in Status} == {
        "NEW": "new",
        "UNMAINTAINED": "unmaintained",
        "DROPPED": "dropped",
        "NONE": "none",
        "OUTDATED": "outdated",
    }


def test_status_members_are_strings() -> None:
    assert isinstance(Status.NEW, str)


@pytest.mark.parametrize("member", list(Status), ids=lambda member: member.name)
def test_status_member_formats_as_its_bare_csv_cell(member: Status) -> None:
    # A plain Enum would render as "Status.NEW" here, which is the defect this
    # guards: csv.writer calls str() on whatever it is handed. Both forms are
    # asserted because they diverge under `class Status(str, Enum)`.
    assert str(member) == member.value
    assert f"{member}" == member.value


def test_status_row_fields_are_named_and_ordered_like_the_csv_columns() -> None:
    assert [field.name for field in dataclasses.fields(StatusRow)] == [
        "package",
        "sle15_owners",
        "slfo_maintainers",
        "status",
    ]


def test_status_row_holds_the_tagged_owner_sets_of_both_sides() -> None:
    row = StatusRow(
        package="ethtool",
        sle15_owners=frozenset({"user-a"}),
        slfo_maintainers=frozenset({"group:team-a"}),
        status=Status.OUTDATED,
    )

    assert row.package == "ethtool"
    assert row.sle15_owners == frozenset({"user-a"})
    assert row.slfo_maintainers == frozenset({"group:team-a"})
    assert row.status is Status.OUTDATED


def test_status_row_is_frozen() -> None:
    row = StatusRow("spice", frozenset({"group:team-a"}), frozenset(), Status.DROPPED)

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.status = Status.NONE  # type: ignore[misc]


def test_rows_compare_by_exact_set_equality_not_by_owner_ordering() -> None:
    # Plan section 3 fixes equality as exact set equality on tagged names, and
    # a real 16 column can carry several owners in one cell. This guards the
    # deliberate divergence from the sibling project, which used a tuple here.
    first = StatusRow(
        "crmsh", frozenset({"user-a"}), frozenset({"user-a", "user-b"}), Status.OUTDATED
    )
    second = StatusRow(
        "crmsh", frozenset({"user-a"}), frozenset({"user-b", "user-a"}), Status.OUTDATED
    )

    assert first == second
