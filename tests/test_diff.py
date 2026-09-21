"""Tests for running the ownership diff and writing it out as CSV."""

import json
import re
from pathlib import Path

import pytest

from bugowner_diff.commands.diff import run_diff
from bugowner_diff.exceptions import DataSourceError

EXPECTED_HEADER = "package,15,16,change"


class _FakePackageList:
    """Answer with a fixed, ordered list, the way the input file's reader does."""

    def __init__(self, packages: list[str]) -> None:
        self._packages = packages

    def load(self, file_path: Path) -> list[str]:
        return self._packages


class _FakeProject:
    """Answer with a fixed project listing, the way the OBS project search does."""

    def __init__(self, packages: frozenset[str]) -> None:
        self._packages = packages

    def list_packages(self, project: str) -> frozenset[str]:
        return self._packages


class _FakeOwners:
    """Answer with fixed owners, and record which packages were asked about.

    A package the fixture did not configure raises ``KeyError`` rather than
    answering with an empty set: an unexpected owner search is the thing the
    listing conditional exists to prevent, and a fake that shrugged it off would
    hide exactly that regression.
    """

    def __init__(self, owners: dict[str, frozenset[str]]) -> None:
        self._owners = owners
        self.asked: list[str] = []

    def find_owners(self, package: str) -> frozenset[str]:
        self.asked.append(package)
        return self._owners[package]


class _FakeArchive:
    """Answer with a fixed document, the way the SLFO git remote does."""

    def __init__(self, document: bytes) -> None:
        self._document = document

    def fetch_maintainership(self, ref: str) -> bytes:
        return self._document


def _document(packages: dict[str, object]) -> bytes:
    """Encode a maintainership document the way the git remote answers with it.

    ``header`` and ``project`` ride along because the real document has them and
    the parser this command calls goes on ignoring them.
    """
    payload = {"header": {"version": 1}, "project": {"name": "SLFO"}, "packages": packages}
    return json.dumps(payload).encode()


def _run(
    packages: list[str],
    listing: frozenset[str],
    owners: dict[str, frozenset[str]],
    maintainers: dict[str, object],
    *,
    output_path: Path | None = None,
) -> _FakeOwners:
    """Run one diff over hand-written sources and hand back the owner fake.

    The owner fake is what comes back because it is the only source that records
    anything: the round-trip contract is asserted on it.
    """
    owner_repository = _FakeOwners(owners)
    run_diff(
        package_list=_FakePackageList(packages),
        project_repository=_FakeProject(listing),
        owner_repository=owner_repository,
        archive_repository=_FakeArchive(_document(maintainers)),
        input_path=Path("16.pkg"),
        output_path=output_path,
        project="SUSE:SLE-15-SP7:GA",
        ref="main",
    )
    return owner_repository


def test_run_diff_writes_the_header_then_every_measured_status_as_a_row_in_input_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # One fixture over all five statuses the measured 119-package run produces:
    # 49 none, 35 changed, 31 added, 3 adopted, 1 removed. `unmaintained` is the
    # sixth value of the vocabulary and is deliberately absent -- it needs a 16
    # entry that names nobody, which no measured entry is. The name says
    # `every_measured_status` because two of these rows are the only place a
    # contract is pinned, and a later trim of the fixture that dropped one would
    # otherwise leave the name still looking right.
    #
    # Two rows carry the contract the rest of the file cannot see. `spice` is
    # absent from the maintainership document, and only a `snapshot.get` without
    # a default tells that apart from an entry naming nobody -- with a
    # `frozenset()` default it would report as `unmaintained`, which is
    # plausible, complete and wrong. `SDL3` is absent from the listing and still
    # shows a 16 owner, as 28 of the 31 measured `added` rows do.
    _run(
        ["abseil-cpp", "blktrace", "spice", "catatonit", "SDL3"],
        frozenset({"abseil-cpp", "blktrace", "spice", "catatonit"}),
        {
            "abseil-cpp": frozenset({"group:team-a"}),
            "blktrace": frozenset({"user-a"}),
            "spice": frozenset({"group:team-a"}),
            "catatonit": frozenset(),
        },
        {
            "abseil-cpp": {"users": [], "groups": ["team-a"]},
            "blktrace": {"users": ["user-b"], "groups": []},
            "catatonit": {"users": ["user-c"], "groups": []},
            "SDL3": {"users": [], "groups": ["team-a"]},
        },
    )

    assert capsys.readouterr().out == (
        f"{EXPECTED_HEADER}\n"
        "abseil-cpp,group:team-a,group:team-a,none\n"
        "blktrace,user-a,user-b,changed\n"
        "spice,group:team-a,,removed\n"
        "catatonit,,user-c,adopted\n"
        "SDL3,,group:team-a,added\n"
    )


def test_run_diff_joins_each_owner_cell_with_a_space_in_sorted_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Sorting is required, not cosmetic: a row holds frozensets, whose iteration
    # order depends on the hash seed and therefore changes between runs. Three
    # owners per cell leave an unsorted writer one chance in six of matching by
    # accident, so this fails loudly rather than never.
    _run(
        ["crmsh"],
        frozenset({"crmsh"}),
        {"crmsh": frozenset({"user-c", "user-a", "user-b"})},
        {"crmsh": {"users": ["user-c", "user-a"], "groups": ["team-b"]}},
    )

    assert capsys.readouterr().out == (
        f"{EXPECTED_HEADER}\ncrmsh,user-a user-b user-c,group:team-b user-a user-c,changed\n"
    )


def test_run_diff_searches_owners_only_for_packages_the_project_listing_holds() -> None:
    # The conditional this pins is what keeps the measured run at 88 network
    # round-trips instead of 119, and it is also the only place the "absent on
    # the 15 side" fact is established -- which is why `classify` is handed None
    # rather than being left to test membership itself.
    #
    # `ethtool` is in the listing and not in the input file: a listing holds
    # thousands of packages the diff never asks about.
    owner_repository = _run(
        ["spice", "SDL3"],
        frozenset({"spice", "ethtool"}),
        {"spice": frozenset({"group:team-a"})},
        {"spice": {"users": [], "groups": ["team-a"]}, "SDL3": {"users": ["user-a"]}},
    )

    assert owner_repository.asked == ["spice"]


def test_run_diff_writes_to_the_given_file_and_to_stdout_without_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both destinations carry the same report; only where it lands differs. The
    # file is read back as UTF-8 because that is what the writer opens it as,
    # while stdout keeps the locale's codec -- an asymmetry no cell can observe,
    # since the owner check leaves nothing non-ASCII to encode.
    arguments = (
        ["crmsh"],
        frozenset({"crmsh"}),
        {"crmsh": frozenset({"user-a"})},
        {"crmsh": {"users": ["user-a"], "groups": []}},
    )
    output_path = tmp_path / "diff.csv"

    _run(*arguments, output_path=output_path)
    _run(*arguments)

    expected = f"{EXPECTED_HEADER}\ncrmsh,user-a,user-a,none\n"
    assert output_path.read_text(encoding="utf-8") == expected
    assert capsys.readouterr().out == expected


def test_run_diff_leaves_no_file_behind_when_a_later_row_cannot_be_rendered(
    tmp_path: Path,
) -> None:
    # Every cell is rendered before the destination is opened, so a refused name
    # ends the run with no artifact at all. Writing row by row would instead
    # leave a header and one good row on disk: a truncated report that reads as
    # a finished one, which is the partial answer fail-fast exists to prevent.
    # `crmsh` renders cleanly and comes first, so only ordering decides whether
    # anything reaches the file; `spice` is the row that fails.
    output_path = tmp_path / "diff.csv"

    with pytest.raises(DataSourceError):
        _run(
            ["crmsh", "spice"],
            frozenset({"crmsh", "spice"}),
            {"crmsh": frozenset({"user-a"}), "spice": frozenset({"=1+1"})},
            {
                "crmsh": {"users": ["user-a"], "groups": []},
                "spice": {"users": ["user-b"], "groups": []},
            },
            output_path=output_path,
        )

    assert not output_path.exists()


@pytest.mark.parametrize(
    ("column", "hostile", "source"),
    [
        ("15", "=1+1", "the OBS owner search"),
        ("16", "user-a,user-b", "_maintainership.json"),
        ("15", "user-a\nuser-b", "the OBS owner search"),
        ("16", "\ud800bad", "_maintainership.json"),
        ("16", "-1-2", "_maintainership.json"),
    ],
    ids=[
        "formula-in-15",
        "comma-in-16",
        "newline-in-15",
        "surrogate-in-16",
        "leading-hyphen-in-16",
    ],
)
def test_run_diff_refuses_an_owner_name_that_cannot_be_rendered_in_a_csv_cell(
    column: str, hostile: str, source: str
) -> None:
    # One message, so one test. The four names stand for the four ways a cell
    # goes wrong -- a spreadsheet formula, a split cell, a split row, and a lone
    # surrogate that `json.loads` accepts and `encode` would later refuse
    # outside this project's error taxonomy -- and both columns appear, because
    # the two call sites name different sources and each has to say so.
    # Enumerating every dangerous character is what the allowlist exists to make
    # unnecessary.
    expected = (
        f"Owner name in the {column} column of package 'crmsh' cannot be rendered "
        f"in a CSV cell: expected a non-empty [A-Za-z0-9._:-] that does not start with "
        f"'-', got {hostile!r} from {source}."
    )

    with pytest.raises(DataSourceError, match=re.escape(expected)):
        _run(
            ["crmsh"],
            frozenset({"crmsh"}),
            {"crmsh": frozenset({hostile if column == "15" else "user-a"})},
            {"crmsh": {"users": [hostile if column == "16" else "user-b"], "groups": []}},
        )
