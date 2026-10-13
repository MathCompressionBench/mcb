# ABOUTME: Locations of lcbench's non-Python parts: the participant runtime copied into packages and the Lean tools.
# ABOUTME: Everything is resolved from this file, so a repo checkout works from any directory (bin/lcbench or python3 -m).
from __future__ import annotations

from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]

# copied verbatim into every task package's .benchmark/ directory
RUNTIME_DIR = PACKAGE_DIR / "runtime"
BENCHKIT = RUNTIME_DIR / "benchkit.py"

# the Lake package `lcbench_tools` (lean/): the organizer's compiled tools lcbench-metric and lcbench-check
LEAN_DIR = REPO_ROOT / "lean"

# compiled metric counter (A, dag_nodes_v1), built by `lake build` in lean/
METRIC_EXE = LEAN_DIR / ".lake" / "build" / "bin" / "lcbench-metric"
