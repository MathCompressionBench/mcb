# ABOUTME: `lcbench audit-package <tar>`: checks a task package tarball for leaks before it is published.
# ABOUTME: Rejects .git, compiled artifacts, private paths, shipped originals (no_hint) and a non-canonical tarball.
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

from .make_task import declares_any
from .runtime import benchkit as bk

# private machine or cluster locations that must never appear in a published package
LEAK_RE = re.compile(rb"/share/data|/scratch/|/mnt/|/Users/|\.codex|/private/tmp")  # hygiene-allow
COMPILED_SUFFIXES = (".olean", ".ilean", ".olean.private", ".olean.server", ".ir", ".c", ".o", ".so", ".dylib")


def _original_hashes(task: dict, task_source: Path | None) -> set[str]:
    hashes = set(task.get("source", {}).get("original_sources_sha256", {}).values())
    if task_source is not None:
        src = json.loads(task_source.read_text())
        hashes |= {e["original_sha256"] for e in src["editable"]}
    return hashes


def erasure_recorded(nh: dict, targets: list[str]) -> bool:
    """TASK.json `no_hint` (written by make-task) accounts for every target: each one erased to `sorryAx`, or, for a
    task with `no_hint.keep_rfl_proofs`, erased except the listed targets whose whole proof is `rfl`."""
    kept = nh.get("rfl_proofs_kept")
    if kept is None:
        return bool(nh.get("every_target_uses_sorryAx")) and nh.get("erased_targets") == len(targets)
    return (bool(nh.get("every_erased_target_uses_sorryAx")) and set(kept) <= set(targets)
            and len(set(kept)) == len(kept) and nh.get("erased_targets") == len(targets) - len(kept))


def audit_tree(top: Path, task_source: Path | None = None, warm_task: dict | None = None) -> tuple[dict, list[str]]:
    """Problems of an unpacked package directory; returns (TASK.json, problems)."""
    problems = list(bk.check_manifest(top))
    task_path = top / "TASK.json"
    if not task_path.is_file():
        return {}, problems + ["TASK.json missing"]
    task = json.loads(task_path.read_text())
    for rel, digest in task.get("tooling_sha256", {}).items():
        if not (top / rel).is_file() or bk.sha256_file(top / rel) != digest:
            problems.append(f"tooling file differs from TASK.json: {rel}")
    originals = _original_hashes(task, task_source) if task.get("mode") == "no_hint" else set()
    targets = [t["name"] for t in task.get("targets", [])]
    for rel in bk.list_files(top):
        parts = rel.split("/")
        if ".git" in parts:
            problems.append(f"git metadata shipped: {rel}")
        if rel.endswith(COMPILED_SUFFIXES) or ".lake" in parts:
            problems.append(f"compiled artifact shipped: {rel}")
        data = (top / rel).read_bytes()
        # benchkit's canonical build root is a fixed public path, not a machine location
        hit = LEAK_RE.search(data.replace(str(bk.CANONICAL_BUILD_ROOT).encode(), b""))
        if hit:
            problems.append(f"path leak in {rel}: {hit.group(0).decode()}")
        # an editable module without targets ships verbatim in no_hint (the slt tasks' support modules); an original
        # that declares a target would ship that target's proof
        if originals and bk.sha256_bytes(data) in originals and declares_any(data.decode(errors="replace"), targets):
            problems.append(f"original owner source shipped: {rel}")
    if task.get("mode") == "no_hint" and not erasure_recorded(task.get("no_hint", {}), targets):
        problems.append("no_hint package does not record that every target was erased to sorryAx")
    if warm_task is not None:
        if warm_task.get("targets") != task.get("targets"):
            problems.append("target statements differ from the warm package")
        if warm_task.get("frozen") != task.get("frozen"):
            problems.append("frozen closures differ from the warm package")
        if warm_task.get("task_id") != task.get("task_id") or warm_task.get("mode") != "warm":
            problems.append("--warm is not the warm package of the same task")
    return task, problems


def audit(tar: Path, task_source: Path | None = None, warm_tar: Path | None = None) -> dict:
    report = {"package": tar.name, "sha256": bk.sha256_file(tar), "mode": None, "problems": []}
    with tempfile.TemporaryDirectory(prefix="lcbench-audit-") as tmp:
        tmp = Path(tmp)
        try:
            top = bk.safe_extract(tar, tmp / "pkg")
        except (ValueError, OSError) as e:
            report["problems"].append(f"unsafe or unreadable tarball: {e}")
            report["ok"] = False
            return report
        canonical = bk.deterministic_tar(top, tmp / "canonical.tar.gz", top.name)
        if canonical != report["sha256"]:
            report["problems"].append(f"tarball is not canonical (a deterministic re-tar gives {canonical})")
        warm_task = None
        if warm_tar is not None:
            wtop = bk.safe_extract(warm_tar, tmp / "warm")
            warm_task = json.loads((wtop / "TASK.json").read_text())
        task, problems = audit_tree(top, task_source, warm_task)
        report["mode"] = task.get("mode")
        report["task_sha256"] = bk.sha256_file(top / "TASK.json") if (top / "TASK.json").is_file() else None
        report["problems"] += problems
    report["ok"] = not report["problems"]
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcb audit-package")
    ap.add_argument("tarball", type=Path, help="task package tarball written by make-task")
    ap.add_argument("--task-source", type=Path, help="tasks/<id>/task_source.json (adds its original hashes)")
    ap.add_argument("--warm", type=Path, help="the warm package of the same task (no_hint statements must match)")
    args = ap.parse_args(argv)
    report = audit(args.tarball.resolve(), args.task_source, args.warm)
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if report["ok"] else 1


COMMANDS = {"audit-package": ("check a task package tarball for leaks (.git, .olean, originals, private paths)", main)}


if __name__ == "__main__":
    sys.exit(main())
