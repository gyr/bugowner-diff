"""Diff package bugownership between an IBS project and the SLFO maintainership file."""

from importlib.metadata import PackageNotFoundError, version

_FALLBACK_VERSION = "0.0.0+unknown"


def _resolve_version() -> str:
    """Return the installed distribution version, or a sentinel if it is not installed."""
    try:
        return version("bugowner-diff")
    except PackageNotFoundError:
        # Reached when src/ is on sys.path directly instead of going through
        # `uv sync`: there is no metadata to read and no git query to make here.
        # This sentinel is NOT the hatch-vcs fallback-version -- hatch-vcs feeds
        # that through the setuptools-scm version scheme, which decorates it into
        # something like 0.0.1.dev0+unknown.d20260916. The two never match.
        return _FALLBACK_VERSION


__version__ = _resolve_version()

__all__ = ["__version__"]
