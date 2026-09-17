"""Tests for the bugowner-diff exception taxonomy."""

import pytest

from bugowner_diff import exceptions
from bugowner_diff.exceptions import (
    BugownerDiffError,
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)


def test_base_error_derives_from_exception() -> None:
    assert issubclass(BugownerDiffError, Exception)


def _defined_error_names() -> set[str]:
    """Return the names of every exception class the module defines."""
    return {
        name
        for name, attribute in vars(exceptions).items()
        if isinstance(attribute, type) and issubclass(attribute, BaseException)
    }


def test_module_defines_exactly_the_five_documented_errors() -> None:
    # The taxonomy is flat and closed by design: the CLI maps each concrete type
    # onto one exit code, so a sixth type added without a mapping would exit 1
    # while claiming to be classified. Without this test that addition is
    # invisible.
    assert _defined_error_names() == {
        "BugownerDiffError",
        "MissingBinaryError",
        "NetworkTimeoutError",
        "InputError",
        "DataSourceError",
    }


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


def test_input_error_is_catchable_as_the_base_error() -> None:
    # The CLI's one `except BugownerDiffError` arm is what turns a raise into an
    # exit code; a type outside the tree prints a traceback at the user instead.
    with pytest.raises(BugownerDiffError):
        raise InputError("invalid project name: '-rf'")


def test_input_error_carries_the_message_it_was_given() -> None:
    # Unlike the two older types this one takes a free-form message: what makes
    # an input unusable differs per call site, and the message is what reaches
    # stderr. A required-argument constructor here would force every call site
    # to invent a label.
    assert str(InputError("invalid project name")) == "invalid project name"


def test_data_source_error_is_catchable_as_the_base_error() -> None:
    with pytest.raises(BugownerDiffError):
        raise DataSourceError("osc api exited 1")


def test_data_source_error_carries_the_message_it_was_given() -> None:
    assert str(DataSourceError("osc api exited 1")) == "osc api exited 1"


def test_no_concrete_error_subclasses_another_concrete_error() -> None:
    # `except` matches by subclass, so a concrete type sitting under another one
    # would let one handler swallow a failure mode meant for a different exit
    # code. Written over the whole module rather than over a hand-listed pair so
    # a type added later is covered without anyone remembering to extend it.
    concrete = sorted(_defined_error_names() - {"BugownerDiffError"})

    for name in concrete:
        # Under the base as well as beside its siblings. The loop below only
        # rules out the wrong parent; on its own it would pass happily for a
        # type that descends from Exception directly and is therefore caught by
        # no arm of the CLI's ladder at all.
        assert issubclass(getattr(exceptions, name), BugownerDiffError), (
            f"{name} must descend from BugownerDiffError"
        )
        for other in concrete:
            if name == other:
                continue
            assert not issubclass(getattr(exceptions, name), getattr(exceptions, other)), (
                f"{name} must not be a subclass of {other}"
            )
