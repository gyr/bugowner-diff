"""Tests for the bugowner_diff package marker."""

import importlib.metadata

import pytest

import bugowner_diff
from bugowner_diff import _FALLBACK_VERSION, _resolve_version


def test_package_exposes_a_non_empty_version_string() -> None:
    assert isinstance(bugowner_diff.__version__, str)
    assert bugowner_diff.__version__ != ""


def test_version_matches_the_installed_distribution_metadata() -> None:
    assert bugowner_diff.__version__ == importlib.metadata.version("bugowner-diff")


def test_version_falls_back_to_the_sentinel_when_the_distribution_is_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_package_not_found(distribution_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(distribution_name)

    monkeypatch.setattr(bugowner_diff, "version", raise_package_not_found)

    assert _resolve_version() == _FALLBACK_VERSION
