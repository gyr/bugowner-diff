"""Tests for the process exit-code taxonomy."""

import sys
from enum import IntEnum

import pytest

from bugowner_diff.exit_codes import ExitCode


def test_exit_code_defines_exactly_the_documented_name_to_value_mapping() -> None:
    # The numeric values are the shell-facing contract; the sibling project's
    # ISSUES = 2 is deliberately absent (2 is argparse's own usage code).
    assert {member.name: member.value for member in ExitCode} == {
        "OK": 0,
        "ERROR": 1,
        "USAGE": 64,
        "TIMEOUT": 124,
        "MISSING_BINARY": 127,
        "INTERRUPT": 130,
    }


def test_exit_code_is_an_int_enum() -> None:
    assert issubclass(ExitCode, IntEnum)
    assert isinstance(ExitCode.ERROR, int)


@pytest.mark.parametrize("member", list(ExitCode), ids=lambda member: member.name)
def test_sys_exit_carries_a_member_that_equals_its_numeric_value(member: ExitCode) -> None:
    with pytest.raises(SystemExit) as raised:
        sys.exit(member)

    # sys.exit stores the member itself, not an int, so this is an IntEnum
    # equality check -- it is what fails if the base class stops being IntEnum.
    assert int(raised.value.code) == member.value
