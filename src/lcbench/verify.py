# ABOUTME: `lcbench verify` (organizer or anyone): from a task package + bundle, rebuilds in a sandbox, re-checks and
# ABOUTME: re-scores from the kernel export, which must equal the bundled one; writes a hash-chained lcbench.receipt.v1.
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import receipt as rc
from . import sandbox as sbx
from .paths import BENCHKIT, LEAN_DIR, REPO_ROOT
from .runtime import benchkit as bk

RECEIPT_SCHEMA = rc.RECEIPT_SCHEMA
SIGNATURE_NAMESPACE = rc.SIGNATURE_NAMESPACE
# (label, file): the label is what the verifier hash records, independent of where the file lives in the repo
VERIFIER_FILES = [("verify.py", Path(__file__).resolve()), ("receipt.py", Path(rc.__file__).resolve()),
                  ("benchkit.py", BENCHKIT)] + \
    [(f"tools/{rel}", LEAN_DIR / rel) for rel in bk.METRIC_TOOL_FILES]


def scorer_sha256() -> str:
    """sha256 over the scoring code shipped in every package (benchkit.py and the organizer Lean tool sources);
    src/lcbench/scorer_versions.json maps each SCORER_VERSION to it."""
    files = [(label, p) for label, p in VERIFIER_FILES if label not in ("verify.py", "receipt.py")]
    return bk.sha256_bytes("".join(f"{bk.sha256_file(p)}  {label}\n" for label, p in files).encode())


def verifier_sha256() -> str:
    return bk.sha256_bytes("".join(f"{bk.sha256_file(p)}  {label}\n" for label, p in VERIFIER_FILES).encode())


COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def verifier_commit(root: Path = REPO_ROOT) -> str | None:
    """The lcbench commit the verifier runs from: `git rev-parse HEAD` in a checkout, else the stamp in
    src/lcbench/_commit (written by `git archive` through export-subst, or by the cluster scripts' code snapshots);
    None when neither names a commit. Dirty trees show in verifier_sha256."""
    if (root / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
        except OSError:
            out = None
        if out is not None and out.returncode == 0 and COMMIT_RE.fullmatch(out.stdout.strip()):
            return out.stdout.strip()
    stamp = root / "src/lcbench/_commit"
    text = stamp.read_text().strip() if stamp.is_file() else ""
    return text if COMMIT_RE.fullmatch(text) else None


def set_of(task_sha: str, sets_dir: Path = REPO_ROOT / "sets") -> str | None:
    """The frozen set (sets/*.json) that lists this TASK.json sha256, or None (toy task, unreleased package)."""
    for path in sorted(sets_dir.glob("*.json")):
        data = json.loads(path.read_text())
        if any(t.get("task_json_sha256") == task_sha for t in data.get("tasks", [])):
            return data["set"]
    return None


def environment(sandbox: str) -> dict:
    """The receipt's environment block (outside receipt_root). Receipts are published, so it names no machine: no
    host name (nor a hash of one) and no absolute path."""
    return {"os": platform.platform(), "python": platform.python_version(),
            "sandbox": sandbox, "build_path": None, "canonical_build_root": None, "timings_s": {}}


def public_build_path(path: str | None) -> dict:
    """Where the build happened, relative to its build root (`<task sha256[:12]>/project`), and whether that root was
    the canonical one, which the .olean bytes depend on. The absolute path names the machine's directories, so the
    receipt does not record it."""
    if path is None:
        return {"build_path": None, "canonical_build_root": None}
    p = Path(path)
    return {"build_path": "/".join(p.parts[-2:]), "canonical_build_root": p.parents[1] == bk.CANONICAL_BUILD_ROOT}


def allowed_bundle_file(rel: str, task: dict) -> bool:
    """SUBMISSION.json, the manifest, the task's scored export file, the editable sources and an optional log."""
    if rel in ("SUBMISSION.json", "MANIFEST.sha256", bk.bundle_export_name(task)) or rel.startswith("trajectory/"):
        return True
    if rel.startswith("source/"):
        return rel[len("source/"):] in task["project"]["editable"]
    return False


def run_report(sub: dict) -> tuple[dict, list[str]]:
    """The receipt's `run` block from SUBMISSION.json metadata.run: the budget class the result competes in, the
    elapsed wall time and the declared tokens. A bundle without run metadata declared no budget: class unlimited."""
    run = (sub.get("metadata") or {}).get("run")
    if run is None:
        return {"declared": False, "budget_seconds": bk.UNLIMITED, "budget_class": bk.UNLIMITED,
                "started_at": None, "elapsed_seconds": None, "tokens": None}, []
    problems = []
    budget = run.get("budget_seconds") if isinstance(run, dict) else None
    if not (budget == bk.UNLIMITED or (type(budget) is int and budget > 0)):
        problems.append(f"run metadata: budget_seconds must be a positive integer or {bk.UNLIMITED!r}, "
                        f"got {budget!r}")
    run = run if isinstance(run, dict) else {}
    budget_class = f"{budget}s" if type(budget) is int else bk.UNLIMITED
    return {"declared": True, "budget_seconds": budget, "budget_class": budget_class,
            "started_at": run.get("started_at"), "elapsed_seconds": run.get("elapsed_seconds"),
            "tokens": run.get("tokens")}, problems


def mathlib_rev(task: dict) -> str | None:
    return next((p["rev"] for p in task["project"].get("packages", []) if p["name"] == "mathlib"), None)


SAFETY_CAP_VAR = "LCBENCH_SAFETY_WALL_S"
# the reason file of the organizers' per-job memory guard, which sets it in every organizer verification job
GUARD_REASON_VAR = "LCB_GUARD_REASON"
GUARD_KINDS = {"node_low": "the node ran low on memory",
               "over_request": "the job used more memory than it requested"}


def guard_record(environ) -> bytes | None:
    """The memory guard's reason file, which the guard writes (atomically) only when it stops a process of the job;
    None when no guard runs or it has stopped nothing yet."""
    path = environ.get(GUARD_REASON_VAR)
    if not path:
        return None
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def guard_stop_error(before: bytes | None, after: bytes | None) -> str | None:
    """The no-verdict reason when the guard stopped a process of this job while the run went on (its reason file
    changed): a stopped build or check fails for a reason that has nothing to do with the submission, and memory never
    decides validity. A stop in another run of the same job also counts, so such a run is simply done again."""
    if after is None or after == before:
        return None
    try:
        kind = json.loads(after)["stops"][-1]["kind"]
    except (ValueError, KeyError, IndexError, TypeError):
        kind = None
    why = GUARD_KINDS.get(kind, "a memory safety stop")
    return (f"infrastructure stop: the organizers' memory guard stopped a process of this run ({why}); no verdict "
            f"on the submission, run it again on another machine")


def apply_safety_cap(task: dict, environ) -> dict:
    """An organizer's job may lower the wall-clock safety cap (never raise it) through LCBENCH_SAFETY_WALL_S, so that a
    stuck run ends with an ERROR receipt before the job's own time limit kills it without one. The cap never decides
    validity, and TASK.json (and so the task hash) is unchanged."""
    try:
        cap = int(environ.get(SAFETY_CAP_VAR, ""))
    except ValueError:
        return task
    if cap <= 0:
        return task
    limits = dict(task.get("limits") or {})
    limits["safety_wall_s"] = min(cap, int(limits.get("safety_wall_s", bk.SAFETY_WALL_S)))
    return {**task, "limits": limits}


def verify(task_src: Path, bundle: Path, workdir: Path, expected_id: str | None,
           deps: Path | None = None, sandbox: str = "none", paranoid: bool | None = None) -> dict:
    """paranoid: also replay every owner module with stock `leanchecker` (default: LCBENCH_PARANOID=1)."""
    started = time.monotonic()
    guard_before = guard_record(os.environ)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    problems: list[str] = []
    task_dir = bk.safe_extract(task_src, workdir / "task") if task_src.is_file() else task_src
    problems += [f"task package: {p}" for p in bk.check_manifest(task_dir)]
    task_bytes = (task_dir / "TASK.json").read_bytes()
    task_sha, task = bk.sha256_bytes(task_bytes), apply_safety_cap(json.loads(task_bytes), os.environ)
    own_tools = {".benchmark/benchkit.py": bk.sha256_file(BENCHKIT),
                 **{f".benchmark/tools/{rel}": bk.sha256_file(LEAN_DIR / rel) for rel in bk.METRIC_TOOL_FILES}}
    tooling_matches = all(task["tooling_sha256"].get(k) == v for k, v in own_tools.items())
    if not tooling_matches:
        problems.append(f"package tooling differs from this verifier: the package was built by scorer "
                        f"{task.get('scorer_version', '(unrecorded)')}, this verifier is scorer {bk.SCORER_VERSION} "
                        f"(commit {verifier_commit()}); rebuild the package with this verifier's make-task")
    bundle_sha = bk.sha256_file(bundle)
    if expected_id and expected_id != bundle_sha:
        problems.append(f"bundle sha256 {bundle_sha} differs from the registered submission ID {expected_id}")
    receipt = {"schema": RECEIPT_SCHEMA, "release": rc.RELEASE, "set": set_of(task_sha),
               "task_name": task["name"], "task_mode": task.get("mode"), "task_revision": task.get("task_revision"),
               "task_sha256": task_sha, "bundle_sha256": bundle_sha, "track": None,
               "metric_ids": bk.metric_ids(task),
               "toolchain": {"id": task["toolchain"]["id"], "lean_githash": task["toolchain"]["lean_githash"]},
               "mathlib_rev": mathlib_rev(task),
               "verifier": {"scorer_version": bk.SCORER_VERSION, "verifier_sha256": verifier_sha256(),
                            "commit": verifier_commit(),
                            "package_tooling_matches_verifier": tooling_matches},
               "source_tree_sha256": None, "kernel_export_sha256": None, "score": None, "scores": None,
               "lints": [], "log_tail": None,
               "trajectory": {"present": False, "files": 0, "tree_sha256": None}, "run": None,
               "environment": environment(sandbox)}
    try:
        bdir = bk.safe_extract(bundle, workdir / "bundle")
    except (ValueError, OSError, EOFError) as e:
        return finish(receipt, problems + [f"bundle unreadable: {e}"], started)
    problems += [f"bundle: {p}" for p in bk.check_manifest(bdir)]
    problems += [f"bundle: unexpected file {rel}" for rel in bk.list_files(bdir) if not allowed_bundle_file(rel, task)]
    try:
        sub = json.loads((bdir / "SUBMISSION.json").read_text())
    except (OSError, ValueError) as e:
        return finish(receipt, problems + [f"SUBMISSION.json unreadable: {e}"], started)
    if sub.get("schema") != bk.SUBMISSION_SCHEMA:
        problems.append(f"unknown submission schema {sub.get('schema')!r}")
    if sub.get("task_sha256") != task_sha:
        problems.append("submission is for a different task (task_sha256 mismatch)")
    if sub.get("toolchain", {}).get("lean_githash") != task["toolchain"]["lean_githash"]:
        problems.append("submission toolchain differs from the task toolchain")
    receipt["track"] = (sub.get("metadata") or {}).get("track")
    if receipt["track"] not in (None, *bk.TRACKS):
        problems.append(f"unknown track {receipt['track']!r} (allowed: {', '.join(bk.TRACKS)})")
    receipt["run"], run_problems = run_report(sub)
    problems += run_problems

    trajectory = sub.get("trajectory", {"present": False})
    tfiles = [rel[len("trajectory/"):] for rel in bk.list_files(bdir) if rel.startswith("trajectory/")]
    if trajectory.get("present"):
        if trajectory.get("tree_sha256") != bk.tree_sha256(bdir / "trajectory", tfiles):
            problems.append("trajectory files differ from SUBMISSION.json trajectory.tree_sha256")
    elif tfiles:
        problems.append("trajectory files present but SUBMISSION.json says none")
    receipt["trajectory"] = {"present": bool(tfiles), "files": len(tfiles),
                             "tree_sha256": bk.tree_sha256(bdir / "trajectory", tfiles) if tfiles else None}

    limits = bk.task_limits(task)
    res = bk.evaluate(task, task_sha, task_dir / task["project"]["root"], bdir / "source", workdir / "eval",
                      LEAN_DIR, deps, sbx.factory(sandbox, workdir, deps, limits.get("mem_mb")), paranoid=paranoid)
    problems += res["problems"]
    infrastructure = guard_stop_error(guard_before, guard_record(os.environ)) or res.get("infrastructure_error")
    if infrastructure:
        receipt["infrastructure_error"] = infrastructure
    receipt["lints"] = res.get("lints", [])
    receipt["log_tail"] = res.get("log", "")[-4000:] or None
    receipt["environment"].update(**public_build_path(res.get("build_path")), timings_s=res.get("timings", {}),
                                  heartbeats=res.get("heartbeats"), kernel_replay=res.get("kernel_replay_mode"),
                                  peak_rss_mb=res.get("peak_rss_mb"),
                                  peak_rss_source=f"{bk.memory_source()} of the process tree of each toolchain "
                                                  f"command, sampled at most {bk.WATCH_INTERVAL_S} s apart (the value "
                                                  "limits.mem_mb applies to; peak_tree_rss_mb sums resident sets)",
                                  peak_tree_rss_mb=res.get("peak_tree_rss_mb"),
                                  peak_processes=res.get("peak_processes"), peak_disk_mb=res.get("peak_disk_mb"))
    receipt.update(source_tree_sha256=res.get("source_tree_sha256"), score=res["score"], scores=res["scores"],
                   kernel_export_sha256=res.get("kernel_export_sha256"))
    claimed = sub.get("claimed_score")
    if res["score"] and claimed != res["score"]:
        problems.append(f"self-reported score {claimed} differs from recomputed {res['score']}")
    if res["scores"] and "claimed_scores" in sub and sub["claimed_scores"] != res["scores"]:
        problems.append(f"self-reported scores {sub['claimed_scores']} differ from recomputed {res['scores']}")
    if res.get("source_tree_sha256") and sub.get("source_tree_sha256") != res["source_tree_sha256"]:
        problems.append("self-reported source_tree_sha256 differs from the bundled sources")
    if "kernel_export_sha256" in res and not export_matches(task, sub, bdir, res["kernel_export_sha256"]):
        problems.append("recompiled kernel export differs from the bundled export")
    return finish(receipt, problems, started)


def export_matches(task: dict, sub: dict, bdir: Path, rebuilt_sha256: str) -> bool:
    """The score comes from the export rebuilt here; the bundled export file and the one SUBMISSION.json claims must
    both equal it."""
    bundled = bdir / bk.bundle_export_name(task)
    return sub.get("kernel_export_sha256") == rebuilt_sha256 and bundled.is_file() \
        and bk.sha256_file(bundled) == rebuilt_sha256


def finish(receipt: dict, problems: list[str], started: float | None = None) -> dict:
    infrastructure = receipt.pop("infrastructure_error", None)
    if infrastructure:
        # a safety limit (wall clock, memory, processes, disk) or the organizers' memory guard stopped the run: no
        # verdict on the submission, whatever else was found
        verdict, problems = "ERROR", [infrastructure]
    else:
        verdict = "ACCEPT" if not problems else "REJECT"
    receipt.update(verdict=verdict, problems=problems,
                   status=rc.status(verdict, receipt["environment"]["sandbox"]))
    if started is not None:
        receipt["environment"]["timings_s"]["total"] = round(time.monotonic() - started, 1)
        receipt["environment"]["wall_s"] = receipt["environment"]["timings_s"]["total"]
    return rc.seal(receipt)


def recompute_root(receipt: dict) -> str:
    """Re-derive receipt_root from the receipt's own fields (used by log verify)."""
    return rc.recompute_root(receipt)


def sign(path: Path, key: Path) -> Path:
    return rc.sign(path, key)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcbench verify")
    ap.add_argument("task", type=Path, help="task package tarball or directory")
    ap.add_argument("bundle", type=Path)
    ap.add_argument("--expected-id", help="submission ID registered at submission time")
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path, help="receipt JSON path")
    ap.add_argument("--deps", type=Path, help="directory holding the task's pinned Lake packages (read-only is fine)")
    ap.add_argument("--sign-key", type=Path, help="OpenSSH ed25519 private key for the receipt signature")
    ap.add_argument("--sandbox", choices=["auto", "bwrap", "none"], default="auto",
                    help="auto: bwrap on Linux when available, else none (a `none` receipt is never `verified`)")
    ap.add_argument("--paranoid", action="store_true", default=None,
                    help="also replay every owner module with stock leanchecker, one process per module (slow); "
                         "a disagreement rejects. Default: LCBENCH_PARANOID=1")
    args = ap.parse_args(argv)
    receipt = verify(args.task.resolve(), args.bundle.resolve(), args.workdir.resolve(), args.expected_id,
                     args.deps.resolve() if args.deps else None, sbx.choose(args.sandbox), args.paranoid)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(bk.canonical_json(receipt))
    s = receipt["score"]
    print(f"{receipt['verdict']} ({receipt['status']}, sandbox {receipt['environment']['sandbox']})  "
          f"task {receipt['task_sha256'][:16]}  bundle {receipt['bundle_sha256'][:16]}  "
          f"score {s and s['total']}/{s and s['baseline']} ({s and s['reduction_pct']}%)  root {receipt['receipt_root'][:16]}")
    for p in receipt["problems"]:
        print("  PROBLEM:", p)
    for p in receipt["lints"]:
        print("  LINT (not a rejection):", p)
    if args.sign_key:
        print("signature:", sign(args.out, args.sign_key))
    if receipt["verdict"] == "ERROR":
        return bk.INFRASTRUCTURE_EXIT
    return 0 if receipt["verdict"] == "ACCEPT" else 1


COMMANDS = {"verify": ("re-build, re-check and re-score a submission bundle; write a hash-chained receipt", main)}


if __name__ == "__main__":
    sys.exit(main())
