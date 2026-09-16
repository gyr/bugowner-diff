"""Tests for the bugowner-diff exception taxonomy."""

import pytest

from bugowner_diff import exceptions
from bugowner_diff.exceptions import (
    BugownerDiffError,
    MissingBinaryError,
    NetworkTimeoutError,
)


def test_base_error_derives_from_exception() -> None:
    assert issubclass(BugownerDiffError, Exception)


def test_module_defines_exactly_the_three_documented_errors() -> None:
    defined = {
        name
        for name, attribute in vars(exceptions).items()
        if isinstance(attribute, type) and issubclass(attribute, BaseException)
    }

    assert defined == {"BugownerDiffError", "MissingBinaryError", "NetworkTimeoutError"}


def test_missing_binary_error_is_catchable_as_the_base_error() -> None:
    with pytest.raises(BugownerDiffError):
        raise MissingBinaryError("osc")


def test_missing_binary_error_stores_the_binary_name() -> None:
    error = MissingBinaryError("osc")

    assert error.binary == "osc"


def test_missing_binary_error_message_names_the_binary() -> None:
    assert str(MissingBinaryError("git")) == "Required binary not found: git"


def test_network_timeout_error_is_catchable_as_the_base_error() -> None:
    with pytest.raises(BugownerDiffError):
        raise NetworkTimeoutError("osc api", 30.0)


def test_network_timeout_error_stores_the_label_and_the_timeout() -> None:
    error = NetworkTimeoutError("osc api", 30.0)

    assert error.label == "osc api"
    assert error.timeout == 30.0


@pytest.mark.parametrize(
    ("timeout", "rendered"),
    [
        (30.0, "30"),
        (2.5, "2.5"),
        (0.5, "0.5"),
        (120.0, "120"),
    ],
)
def test_network_timeout_error_message_renders_the_timeout_compactly(
    timeout: float, rendered: str
) -> None:
    error = NetworkTimeoutError("git archive", timeout)

    assert str(error) == f"git archive timed out after {rendered}s"


def test_the_two_concrete_errors_are_siblings_rather_than_a_hierarchy() -> None:
    # `except` matches by subclass, so this is what keeps one handler from
    # swallowing the other failure mode.
    assert not issubclass(MissingBinaryError, NetworkTimeoutError)
    assert not issubclass(NetworkTimeoutError, MissingBinaryError)
