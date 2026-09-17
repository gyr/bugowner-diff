"""Tests for the repositories package marker."""

from pathlib import Path

from bugowner_diff import repositories


def test_all_lists_every_repository_submodule() -> None:
    # The package deliberately re-exports nothing, so __all__ is documentation
    # of what lives here rather than an import surface -- and documentation that
    # nothing checks goes stale. Commits 7 and 8 each add a module; without this
    # test, one of them silently does not.
    directory = Path(repositories.__file__).parent
    submodules = {path.stem for path in directory.glob("*.py") if not path.stem.startswith("__")}

    assert set(repositories.__all__) == submodules
