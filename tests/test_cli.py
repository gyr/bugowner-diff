"""Tests for the command-line entry point and its failure-to-exit-code ladder.

The ladder is enumerated once, in :data:`_RUNGS`, and read by three tests: the
mapping, the taxonomy walk and the traceback property. One constant rather than
three literal lists is deliberate -- a rung added to one list only would be
covered by none of them, which is the regression these tests exist to catch.
"""

import logging
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from bugowner_diff import cli
from bugowner_diff.cli import main
from bugowner_diff.exceptions import (
    BugownerDiffError,
    DataSourceError,
    InputError,
    MissingBinaryError,
    NetworkTimeoutError,
)
from bugowner_diff.exit_codes import ExitCode
from bugowner_diff.repositories.obs_owner_repository import ObsOwnerRepositoryImpl
from bugowner_diff.repositories.obs_project_repository import ObsProjectRepositoryImpl
from bugowner_diff.repositories.package_list_repository import PackageListRepositoryImpl
from bugowner_diff.repositories.remote_archive_repository import RemoteArchiveRepositoryImpl

# A complete argv, so a test about a failure inside the run never trips over the
# parser on its way there. The input path is never opened: every test that gets
# as far as the run replaces `run_diff`, and the parser tests below return
# before the run is reached.
_ARGV = ["-i", "packages.txt", "--project", "SUSE:SLE-15-SP7:GA", "--ref", "slfo-main"]

# One row per handled rung of the ladder, in the source order the arms are
# written in. `DataSourceError` is the row for the `BugownerDiffError` arm: it is
# the one concrete member of the taxonomy that has no arm of its own, so naming
# it here is both what exercises the base arm and what lets the taxonomy walk
# below compare two sets of concrete types.
#
# The catch-all `Exception` arm is deliberately absent: it is the one rung whose
# contract is to be loud, so it is driven by `_BoomError` in its own test rather
# than by a row here that every "no traceback" assertion would then have to
# exempt.
_RUNGS: tuple[tuple[BaseException, ExitCode], ...] = (
    (KeyboardInterrupt(), ExitCode.INTERRUPT),
    (BrokenPipeError(32, "Broken pipe"), ExitCode.ERROR),
    (MissingBinaryError("osc"), ExitCode.MISSING_BINARY),
    (NetworkTimeoutError("osc api", 30.0), ExitCode.TIMEOUT),
    (InputError("the package list holds no packages"), ExitCode.USAGE),
    (DataSourceError("osc api exited 1"), ExitCode.ERROR),
    (OSError(13, "Permission denied"), ExitCode.ERROR),
)


class _BoomError(Exception):
    """A failure from outside this project's taxonomy, i.e. a bug in the tool."""


def _rung_id(failure: object) -> str:
    """Name a parametrised case after the exception type it drives."""
    return type(failure).__name__


@pytest.fixture(autouse=True)
def _keep_fd_1_open(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop the BrokenPipeError rung pointing this process's fd 1 at /dev/null.

    The rung's ``os.dup2`` is correct in a real run and fatal in a test run: it
    would replace pytest's own captured stdout for the rest of the session. The
    call is still made and still covered -- only its effect on this process is
    neutralised.
    """
    monkeypatch.setattr(os, "dup2", lambda *args: None)
    yield


def _raising(exc: BaseException) -> Callable[..., None]:
    """Return a ``run_diff`` stand-in that fails with ``exc``."""

    def _run(**kwargs: Any) -> None:
        raise exc

    return _run


def _descendants(cls: type[BaseException]) -> Iterator[type[BaseException]]:
    """Yield every subclass of ``cls``, at any depth."""
    for subclass in cls.__subclasses__():
        yield subclass
        yield from _descendants(subclass)


def test_main_wires_the_repositories_and_the_parsed_arguments_into_run_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "run_diff", _run)

    exit_code = main(
        [
            "-i",
            "packages.txt",
            "-o",
            "report.csv",
            "--project",
            "SUSE:SLE-15-SP7:GA",
            "--ref",
            "slfo-main",
        ]
    )

    assert exit_code == ExitCode.OK
    assert isinstance(captured["package_list"], PackageListRepositoryImpl)
    assert isinstance(captured["project_repository"], ObsProjectRepositoryImpl)
    assert isinstance(captured["owner_repository"], ObsOwnerRepositoryImpl)
    assert isinstance(captured["archive_repository"], RemoteArchiveRepositoryImpl)
    assert captured["input_path"] == Path("packages.txt")
    assert captured["output_path"] == Path("report.csv")
    assert captured["project"] == "SUSE:SLE-15-SP7:GA"
    assert captured["ref"] == "slfo-main"


@pytest.mark.parametrize(("failure", "expected"), _RUNGS, ids=_rung_id)
def test_each_failure_maps_to_its_exit_code(
    failure: BaseException, expected: ExitCode, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_diff", _raising(failure))

    assert main(_ARGV) == expected


def test_every_member_of_the_taxonomy_has_a_rung() -> None:
    # Goes red the day a type is added to the taxonomy and not wired into the
    # ladder, which would otherwise be silently caught by the base arm and
    # reported as a generic error.
    wired = {type(failure) for failure, _ in _RUNGS if isinstance(failure, BugownerDiffError)}

    assert set(_descendants(BugownerDiffError)) == wired


@pytest.mark.parametrize("failure", [failure for failure, _ in _RUNGS], ids=_rung_id)
def test_a_handled_failure_prints_no_traceback(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "run_diff", _raising(failure))

    main(_ARGV)

    assert "Traceback" not in capsys.readouterr().err


def test_a_failure_from_outside_the_taxonomy_prints_its_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The other half of the traceback property, and the half that stops someone
    # later "fixing" the catch-all into silence: a bug in the tool must arrive
    # with the stack that produced it.
    monkeypatch.setattr(cli, "run_diff", _raising(_BoomError("unreachable branch reached")))

    assert main(_ARGV) == ExitCode.ERROR

    stderr = capsys.readouterr().err
    assert "Traceback" in stderr
    assert "bug in bugowner-diff" in stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["--project", "SUSE:SLE-15-SP7:GA", "--ref", "slfo-main"],
        ["-i", "packages.txt", "--ref", "slfo-main"],
        ["-i", "packages.txt", "--project", "SUSE:SLE-15-SP7:GA"],
        [*_ARGV, "-d", "-q"],
        [*_ARGV, "--nonesuch"],
    ],
    ids=["no-input", "no-project", "no-ref", "debug-and-quiet", "unknown-flag"],
)
def test_the_parser_maps_argv_it_refuses_to_usage(argv: list[str]) -> None:
    # One row per decision the parser is asked to hold, not per malformed input:
    # three options that carry no default and must therefore be supplied, the two
    # verbosity flags that may not be combined, and an option that does not exist.
    # argparse configuration is declarative, so statement coverage never reaches
    # it -- deleting a `required=True` leaves every other test in this file green.
    assert main(argv) == ExitCode.USAGE


@pytest.mark.parametrize(
    ("flags", "expected_level"),
    [([], logging.WARNING), (["-d"], logging.DEBUG), (["-q"], logging.ERROR)],
    ids=["default", "debug", "quiet"],
)
def test_the_verbosity_flags_choose_the_log_level(
    flags: list[str], expected_level: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What `basicConfig` is asked for, rather than what the root logger ends up
    # with: `basicConfig` does nothing once the root logger has handlers, which
    # it has under pytest, so its effect is not observable from here.
    asked: list[int] = []

    def _basic_config(level: int) -> None:
        asked.append(level)

    def _run(**kwargs: Any) -> None:
        pass

    monkeypatch.setattr(cli.logging, "basicConfig", _basic_config)
    monkeypatch.setattr(cli, "run_diff", _run)

    assert main([*_ARGV, *flags]) == ExitCode.OK
    assert asked == [expected_level]


def test_help_exits_zero_rather_than_becoming_a_return_value() -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])

    assert exit_info.value.code == 0
