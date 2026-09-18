"""Tests for parsing the SLFO maintainership document into tagged owner sets."""

import json
import logging

import pytest

from bugowner_diff.exceptions import DataSourceError
from bugowner_diff.services.maintainership_snapshot import parse_tagged_snapshot


def _document(packages: object) -> bytes:
    """Encode a maintainership document the way the git remote answers with it.

    ``header`` and ``project`` ride along in every fixture because the real
    document has them and this module must go on ignoring them.
    """
    payload = {"header": {"version": 1}, "project": {"name": "SLFO"}, "packages": packages}
    return json.dumps(payload).encode()


def test_parse_tagged_snapshot_tags_users_bare_and_groups_with_the_group_prefix() -> None:
    # The whole happy path in one document: `header` and `project` are read by
    # nothing here, a user keeps its bare spelling while a group gains the
    # `group:` tag the domain module owns, and both lists of one package land in
    # the same set. `crmsh` carries an empty `users` list, which 1655 of the
    # 2981 measured entries really have: the source's answer, not a shape
    # violation. No entry has both lists empty, so this document alone never
    # answers with an empty owner set.
    document = _document(
        {
            "ethtool": {"users": ["user-a"], "groups": ["team-a"]},
            "crmsh": {"users": [], "groups": ["team-a", "team-b"]},
        }
    )

    snapshot = parse_tagged_snapshot(document)

    assert snapshot == {
        "ethtool": frozenset({"user-a", "group:team-a"}),
        "crmsh": frozenset({"group:team-a", "group:team-b"}),
    }
    # Asserted separately because the equality above cannot see it: a set and a
    # frozenset of the same members compare equal -- verified -- so without this
    # the contract's frozenset could regress to a mutable set that a later stage
    # of the diff could edit in place.
    assert all(isinstance(owners, frozenset) for owners in snapshot.values())


def test_parse_tagged_snapshot_reads_an_entry_without_a_groups_key_as_having_no_groups() -> None:
    # Measured in `probe-data/slfo_main.json.gz`: 3 of the 2981 entries --
    # `kernel-source` among them -- carry `users` and no `groups` key at all.
    # That is the document's answer, "this package has no group owner", and not
    # a violation of its shape; refusing it aborted every possible run, because
    # the loop reaches all 2981 entries whatever the input list asks for.
    snapshot = parse_tagged_snapshot(_document({"kernel-source": {"users": ["user-a"]}}))

    assert snapshot == {"kernel-source": frozenset({"user-a"})}


@pytest.mark.parametrize(
    "document",
    [b"not json at all", b'{"packages": {"ethtool": ' + b"[" * 10_000, b'{"packages": \xff}'],
    ids=["not-json", "too-deeply-nested", "invalid-utf-8"],
)
def test_parse_tagged_snapshot_refuses_a_document_that_is_not_readable_json(
    document: bytes,
) -> None:
    # One raise site and one message for the whole JSON corruption set, which the
    # `(ValueError, RecursionError)` pair is exhaustive over here -- verified:
    # json.JSONDecodeError is a ValueError, and so is the UnicodeDecodeError
    # json.loads raises when it decodes the bytes itself, while RecursionError is
    # a RuntimeError and is the reason the pair has a second member at all. The
    # three inputs are the three parents, not three shapes of bad syntax.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(document)

    assert "JSON" in str(caught.value)


def test_parse_tagged_snapshot_refuses_a_document_that_repeats_a_key() -> None:
    # json.loads keeps the last value for a repeated key and says nothing, so
    # without a hook the owners of the first `ethtool` are lost and the diff
    # answers confidently from half a document. Refused rather than merged: the
    # document cannot say which of the two is the package's real owner set.
    document = (
        b'{"packages": {"ethtool": {"users": ["user-a"], "groups": []},'
        b' "ethtool": {"users": ["user-b"], "groups": []}}}'
    )

    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(document)

    assert "ethtool" in str(caught.value)


def test_parse_tagged_snapshot_refuses_a_document_whose_root_is_not_an_object() -> None:
    # Readable JSON that is not the document. Without this the subscript below
    # raises a bare TypeError that names neither the file nor what arrived, and
    # escapes the taxonomy as a bug traceback instead of a named failure.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(b'[{"packages": {}}]')

    assert "list" in str(caught.value)


@pytest.mark.parametrize(
    ("document", "arrived"),
    [
        (json.dumps({"header": {"version": 1}, "project": {"name": "SLFO"}}).encode(), "NoneType"),
        (_document(["ethtool", "crmsh"]), "list"),
    ],
    ids=["absent", "not-an-object"],
)
def test_parse_tagged_snapshot_refuses_a_document_without_a_packages_object(
    document: bytes, arrived: str
) -> None:
    # One condition and one message, not two checks: a document with no
    # `packages` key and one whose `packages` is a list are both "the mapping
    # this reads is not there", and an absent key would otherwise read as a
    # document every package is missing from, reporting the whole project as
    # `dropped`.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(document)

    assert "packages" in str(caught.value)
    assert arrived in str(caught.value)


def test_parse_tagged_snapshot_refuses_a_package_entry_that_is_not_an_object() -> None:
    # The package is named in the message because it is the only way to find the
    # offending line in a 2981-package document.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(_document({"ethtool": ["user-a"]}))

    assert "ethtool" in str(caught.value)
    assert "list" in str(caught.value)


@pytest.mark.parametrize(
    ("entry", "key", "arrived"),
    [
        ({"groups": ["team-a"]}, "users", "NoneType"),
        ({"users": [], "groups": "team-a"}, "groups", "str"),
    ],
    ids=["users-absent", "groups-not-a-list"],
)
def test_parse_tagged_snapshot_refuses_an_owner_key_that_is_not_a_list(
    entry: dict[str, object], key: str, arrived: str
) -> None:
    # The two rows are the two halves of the measured asymmetry. `users` is
    # present in all 2981 entries, so its absence is a shape violation and not
    # an empty owner set -- read as empty it would understate the ownership of
    # the package. `groups` is the key that may be absent, which is exactly why
    # a present-but-wrong `groups` must still be refused rather than swept into
    # the same default. The string case is what a bare `for name in entry[key]`
    # would iterate one character at a time.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(_document({"ethtool": entry}))

    assert "ethtool" in str(caught.value)
    assert key in str(caught.value)
    assert arrived in str(caught.value)


def test_parse_tagged_snapshot_refuses_an_owner_name_that_is_not_a_string() -> None:
    # The narrowing this pins is required, not defensive: tag_user, tag_group and
    # ambiguity_reason all document "already narrowed to str by the caller" as a
    # precondition, and this module is the caller. A non-string reaching
    # tag_group would join the owner set as "group:42" and compare against
    # nothing.
    with pytest.raises(DataSourceError) as caught:
        parse_tagged_snapshot(_document({"ethtool": {"users": [42], "groups": []}}))

    assert "ethtool" in str(caught.value)
    assert "users" in str(caught.value)
    assert "42" in str(caught.value)


@pytest.mark.parametrize(
    "owner",
    ["", "group:team-a"],
    ids=["empty-name", "group-prefixed-user"],
)
def test_parse_tagged_snapshot_warns_about_an_ambiguous_name_and_keeps_it(
    caplog: pytest.LogCaptureFixture, owner: str
) -> None:
    # Reported and kept, never dropped or rewritten: the document is
    # authoritative about the spelling of its own owners, and an empty name
    # inside a `users` list is the source's *answer*, not a violation of its
    # shape -- unlike the OBS side, where a missing name is a broken document.
    # This module is the only remaining caller that can reach the empty class.
    #
    # The group-prefixed user is the class that loses information: it tags to
    # the same string a group named `team-a` produces, so the two collapse into
    # one set member and the warning is the only trace left of the second owner.
    with caplog.at_level(logging.WARNING):
        snapshot = parse_tagged_snapshot(_document({"ethtool": {"users": [owner], "groups": []}}))

    assert snapshot["ethtool"] == frozenset({owner})
    assert f"Owner name {owner!r} renders ambiguously:" in caplog.text
