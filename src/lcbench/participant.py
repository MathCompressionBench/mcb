# ABOUTME: Registers the participant commands (start, check, submit) for `lcbench`; they run the packaged runtime.
# ABOUTME: The same runtime/benchkit.py is copied into every package, so `lcbench check` equals the package's ./check.
from __future__ import annotations

from .runtime import benchkit


def _runtime(cmd: str):
    return lambda argv: benchkit.main([cmd, *argv])


COMMANDS = {
    "start": ("fetch a task package, check its hashes, set up the pinned project", _runtime("start")),
    "check": ("compile, check and score a package's editable sources", _runtime("check")),
    "submit": ("re-check and write a content-addressed submission bundle", _runtime("submit")),
}
