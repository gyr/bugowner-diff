"""Tests for classifying one package's two owner sets into its diff status."""

import pytest

from bugowner_diff.domain.status_row import Status, StatusRow
from bugowner_diff.services.status_service import classify


@pytest.mark.parametrize(
    ("package", "sle15_owners", "slfo_maintainers", "expected"),
    [
        (
            "SDL3",
            None,
            frozenset({"group:team-a"}),
            StatusRow("SDL3", frozenset(), frozenset({"group:team-a"}), Status.NEW),
        ),
        (
            "catatonit",
            frozenset(),
            frozenset({"group:team-a"}),
            StatusRow("catatonit", frozenset(), frozenset({"group:team-a"}), Status.UNMAINTAINED),
        ),
        (
            "spice",
            frozenset({"group:team-a"}),
            None,
            StatusRow("spice", frozenset({"group:team-a"}), frozenset(), Status.DROPPED),
        ),
        (
            "abseil-cpp",
            frozenset({"group:team-a"}),
            frozenset({"group:team-a"}),
            StatusRow(
                "abseil-cpp",
                frozenset({"group:team-a"}),
                frozenset({"group:team-a"}),
                Status.NONE,
            ),
        ),
        (
            "crmsh",
            frozenset({"user-a"}),
            frozenset({"user-b"}),
            StatusRow("crmsh", frozenset({"user-a"}), frozenset({"user-b"}), Status.OUTDATED),
        ),
    ],
    ids=["new", "unmaintained", "dropped", "none", "outdated"],
)
def test_classify_reports_the_status_the_two_owner_sets_imply(
    package: str,
    sle15_owners: frozenset[str] | None,
    slfo_maintainers: frozenset[str] | None,
    expected: StatusRow,
) -> None:
    # The whole ladder in one table, one row per status, each row a shape
    # measured in the live 119-package run. The assertion is on the whole row
    # rather than on `.status`, because the row is where the second half of the
    # contract shows: an absent side becomes an empty cell, and a side that
    # answered is carried through unchanged -- including the 16 column of a
    # `new` package, which 28 of the 31 measured `new` rows actually fill.
    #
    # The `unmaintained` and `none` rows are what makes the empty set and
    # `None` distinct arguments rather than two spellings of "nothing": the
    # first side answered and named nobody, and reading that as absent would
    # report the package as `new`.
    assert classify(package, sle15_owners, slfo_maintainers) == expected


@pytest.mark.parametrize(
    ("package", "sle15_owners", "expected_status"),
    [
        ("hiredis", None, Status.NEW),
        ("package-a", frozenset(), Status.UNMAINTAINED),
    ],
    ids=["absent-from-both", "unowned-on-15-and-absent-from-16"],
)
def test_classify_settles_a_package_absent_from_16_by_its_15_side_first(
    package: str, sle15_owners: frozenset[str] | None, expected_status: Status
) -> None:
    # Both rows have no entry in the maintainership file, and neither is
    # `dropped`: the 15 side is consulted first and answers both of them.
    #
    # The first row is measured -- `hiredis`, `iansible-trento` and
    # `toolbox-branding-SLE` are absent from both sources in the live run -- and
    # a package nobody has ever packaged is `new`, not dropped from a project it
    # was never in. The second row is settled by `Status.DROPPED`'s own
    # docstring, "owned on the 15 side, absent from the maintainership file": a
    # package with no 15-side owner cannot be dropped. It does not occur in the
    # live data, where the one dropped package has a 15-side owner, so the
    # docstring decides it and this test is the only thing pinning the order.
    # It carries a placeholder name for that reason: a measured package name on
    # a shape no measurement produced is how a sample gets read as a contract.
    row = classify(package, sle15_owners, None)

    assert row == StatusRow(package, frozenset(), frozenset(), expected_status)
