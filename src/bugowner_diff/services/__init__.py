"""Pure transformations over what the repositories fetched: parsing, diffing, reporting.

Submodules only. Nothing is re-exported here on purpose: a service is a plain
function over data a repository already produced, and a second import path for
one would let a caller reach it without naming the module it belongs to.
"""

__all__ = ["maintainership_snapshot"]
