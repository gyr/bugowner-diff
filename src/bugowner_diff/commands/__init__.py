"""Orchestration of a diff run: the repositories, the services and the report.

Submodules only. Nothing is re-exported here on purpose: a command is the one
place the injected repositories are wired together, and a second import path for
it would let the CLI reach an orchestration without naming the run it performs.
"""

__all__ = ["diff"]
