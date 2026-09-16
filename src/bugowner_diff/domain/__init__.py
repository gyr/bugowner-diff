"""Domain vocabulary of the ownership diff: statuses, rows, owner-name tagging.

Submodules only. Nothing is re-exported here on purpose: the tag rule owes its
existence to having exactly one import path, and a second name for it in this
package is the first step back towards two definitions.
"""

__all__ = ["owner_name", "status_row"]
