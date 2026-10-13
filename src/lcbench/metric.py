# ABOUTME: `lcbench metric`: runs the compiled Lean counter lcbench-metric inside a built task project (via `lake env`),
# ABOUTME: records wall time and peak RSS, and optionally cross-checks A and dag_nodes_v1 with the Python export reader.
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from lcbench import export_reader
from lcbench.paths import LEAN_DIR, METRIC_EXE

METRIC_IDS = ("expr_nodes_with_levels_v1", "dag_nodes_v1")


def ensure_exe(exe: Path = METRIC_EXE) -> Path:
    """Build lean/ (lake build) if the metric executable is missing."""
    if not exe.is_file():
        proc = subprocess.run(["lake", "build"], cwd=LEAN_DIR, capture_output=True, text=True)
        if proc.returncode != 0 or not exe.is_file():
            raise RuntimeError(f"lake build of {LEAN_DIR} failed:\n{proc.stdout}{proc.stderr}")
    return exe


# Runs one command as the only child of a fresh Python process, so RUSAGE_CHILDREN is exactly that command's
# process tree: its peak RSS is not mixed with earlier builds or tests.
_MEASURE = """import json, resource, subprocess, sys, time
started = time.monotonic()
rc = subprocess.run(sys.argv[2:]).returncode
unit = 1 if sys.platform == "darwin" else 1024
peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * unit
with open(sys.argv[1], "w") as f:
    json.dump({"rc": rc, "wall_s": round(time.monotonic() - started, 3), "peak_rss_bytes": peak}, f)
"""


def measured_run(cmd: list[str], cwd: Path, env: dict | None = None):
    """Run `cmd`; return (CompletedProcess with text stdout/stderr, {wall_s, peak_rss_bytes})."""
    with tempfile.TemporaryDirectory() as tmp:
        stats = Path(tmp) / "stats.json"
        proc = subprocess.run([sys.executable, "-c", _MEASURE, str(stats), *cmd], cwd=cwd, env=env,
                              capture_output=True, text=True)
        measured = json.loads(stats.read_text()) if stats.is_file() else {"wall_s": None, "peak_rss_bytes": None}
    measured.pop("rc", None)
    return proc, measured


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run_metric(request: dict, project: Path, exe: Path | None = None, crosscheck: bool = False,
               env: dict | None = None) -> dict:
    """Run lcbench-metric on `request` with the project's LEAN_PATH. Returns its JSON result plus `environment`
    (wall time, peak RSS of the child processes, export size and sha256) and, with `crosscheck`, the Python
    recount of the export. `env` is the environment for `lake` (default: this process's). Raises RuntimeError if
    the counter fails or the two implementations disagree."""
    exe = ensure_exe() if exe is None else exe
    if crosscheck and not request.get("export"):
        raise ValueError("crosscheck needs request['export']")
    with tempfile.TemporaryDirectory() as tmp:
        request_path = Path(tmp) / "request.json"
        request_path.write_text(json.dumps(request))
        proc, measured = measured_run(["lake", "env", str(exe), str(request_path)], project, env)
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        raise RuntimeError(f"lcbench-metric exit {proc.returncode}:\n{proc.stdout}{proc.stderr}") from None
    if proc.returncode != 0 or "error" in result:
        raise RuntimeError(f"lcbench-metric exit {proc.returncode}: {result.get('error')}\n{proc.stderr}")
    environment = dict(measured, host_platform=sys.platform)
    if request.get("export"):
        export = Path(request["export"])
        environment.update(export_sha256=sha256_file(export))
    result["environment"] = environment
    if crosscheck:
        started = time.monotonic()
        python = export_reader.recount(request["export"])
        python.pop("declarations")
        python["wall_s"] = round(time.monotonic() - started, 3)
        agree = all(python[k] == result["metrics"][k] for k in ("expr_nodes_with_levels_v1", "dag_nodes_v1"))
        result["crosscheck"] = {"python": python, "agree": agree}
        if not agree:
            raise RuntimeError(f"metric implementations disagree: lean={result['metrics']} python={python}")
    return result


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mcb metric",
                                 description="Run lcbench-metric (the proof-term node count and dag_nodes_v1) "
                                             "on a built task project; see docs/METRIC.md for the request format.")
    ap.add_argument("request", help="request JSON: modules, targets, background{prefixes,modules}, optional export")
    ap.add_argument("--project", default=".", help="built Lake project whose `lake env` provides LEAN_PATH")
    ap.add_argument("--crosscheck", action="store_true", help="recount the node count and dag_nodes_v1 from the export in Python")
    ap.add_argument("--exe", help="lcbench-metric executable (default: lean/.lake/build/bin, built if missing)")
    ap.add_argument("--out", help="also write the JSON result here")
    args = ap.parse_args(argv)
    request = json.loads(Path(args.request).read_text())
    try:
        result = run_metric(request, Path(args.project), Path(args.exe) if args.exe else None, args.crosscheck)
    except (RuntimeError, ValueError) as err:
        print(json.dumps({"error": str(err)}))
        return 1
    text = json.dumps(result, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0


COMMANDS = {"metric": ("compute the node count and dag_nodes_v1 of a built task with the compiled Lean counter", main)}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
