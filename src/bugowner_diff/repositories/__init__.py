"""Adapters that read the diff's inputs: the local package list and remote sources.

Submodules only. Nothing is re-exported here on purpose: each repository is a
Protocol plus one Impl, injected at the command layer, and a second import path
for either half invites a second implementation to hide behind it.
"""

__all__ = [
    "obs_owner_repository",
    "obs_project_repository",
    "package_list_repository",
    "remote_archive_repository",
]
