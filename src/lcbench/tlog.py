# ABOUTME: `lcbench log`: a local git repository that simulates the public append-only submission log.
# ABOUTME: Each entry commits one receipt_root and hash-chains to the previous entry; `log verify` finds any rewrite.
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import receipt as rc
from .runtime import benchkit as bk

ENTRY_SCHEMA = "lcbench.log_entry.v1"
GENESIS = "0" * 64
GIT_ID = ["-c", "user.name=lcbench-log", "-c", "user.email=log@lcbench.invalid", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *GIT_ID, "-C", str(repo), *args], capture_output=True, text=True)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    (repo / "README").write_text("MathCompressionBench submission log: entries/NNNNNN.json, one per commit, append-only.\n")
    git(repo, "add", "README")
    git(repo, "commit", "-q", "-m", "log genesis")


def entries(repo: Path) -> list[Path]:
    return sorted((repo / "entries").glob("*.json")) if (repo / "entries").is_dir() else []


def entry_id(entry: dict) -> str:
    """An entry's id: sha256 of its canonical JSON without the id itself."""
    return bk.sha256_bytes(bk.canonical_json({k: v for k, v in entry.items() if k != "entry_id"}))


def append(repo: Path, receipt_path: Path) -> dict:
    """Append one receipt (accepted or rejected). A receipt_root already in the log returns the existing entry
    without a new commit, so re-appending the same verification is idempotent."""
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if rc.recompute_root(receipt) != receipt.get("receipt_root"):
        raise ValueError(f"receipt {receipt_path.name} does not recompute to its receipt_root; refusing to log it")
    if receipt.get("verdict") not in ("ACCEPT", "REJECT"):
        raise ValueError(f"receipt {receipt_path.name} has no verdict ({receipt.get('verdict')}: the run did not "
                         "finish); re-run the verification instead of logging it")
    existing = entries(repo)
    for path in existing:
        old = json.loads(path.read_bytes())
        if old.get("receipt_root") == receipt["receipt_root"]:
            return old
    prev = bk.sha256_file(existing[-1]) if existing else GENESIS
    seq = len(existing) + 1
    entry = {"schema": ENTRY_SCHEMA, "seq": seq, "prev_entry_sha256": prev,
             "receipt_root": receipt["receipt_root"], "receipt_sha256": bk.sha256_bytes(receipt_bytes),
             "task_sha256": receipt["task_sha256"], "bundle_sha256": receipt["bundle_sha256"],
             "verdict": receipt["verdict"], "score_total": (receipt.get("score") or {}).get("total")}
    entry["entry_id"] = entry_id(entry)
    path = repo / "entries" / f"{seq:06d}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(bk.canonical_json(entry))
    git(repo, "add", str(path.relative_to(repo)))
    git(repo, "commit", "-q", "-m", f"entry {seq} receipt_root {receipt['receipt_root']}")
    return entry


def receipt_files(receipts: Path | list[Path]) -> list[Path]:
    """The receipt files under one directory or several, searched recursively: the intake keeps one directory per
    task (receipts/<task>/<id>.json), and a log holds the receipts of every task."""
    dirs = [receipts] if isinstance(receipts, (str, Path)) else list(receipts)
    return sorted(p for d in dirs for p in Path(d).rglob("*.json") if p.is_file())


def verify(repo: Path, pinned_head: str | None = None, receipts: Path | list[Path] | None = None) -> list[str]:
    """Append-only: every commit after genesis adds exactly the next entry file and touches nothing else;
    entries chain by sha256; commit messages carry receipt_root; optional pinned head must be an ancestor. With
    `receipts` (a directory, searched recursively, or a list of them), every entry's receipt must be found there and
    recompute to the logged receipt_root."""
    problems = []
    if git(repo, "status", "--porcelain"):
        problems.append("working tree differs from the committed log")
    commits = git(repo, "rev-list", "--reverse", "--topo-order", "HEAD").split()
    if len(git(repo, "rev-list", "--merges", "HEAD").split()) > 0:
        problems.append("log history contains merge commits")
    prev = GENESIS
    by_receipt_sha = {}
    if receipts is not None:
        for p in receipt_files(receipts):
            by_receipt_sha[bk.sha256_file(p)] = p
    for i, commit in enumerate(commits[1:], start=1):
        changes = [line.split("\t") for line in
                   git(repo, "diff-tree", "--no-commit-id", "--name-status", "-r", commit).splitlines()]
        want = f"entries/{i:06d}.json"
        if changes != [["A", want]]:
            problems.append(f"commit {commit[:12]} is not a single append of {want}: {changes}")
            continue
        blob = git(repo, "show", f"{commit}:{want}").encode()
        head_path = repo / want
        if not head_path.is_file() or head_path.read_bytes() != blob:
            problems.append(f"{want} at HEAD differs from when it was appended")
        entry = json.loads(blob)
        if entry.get("entry_id") != entry_id(entry):
            problems.append(f"chain broken at {i}: {want} content does not match its entry_id")
        if entry.get("seq") != i or entry.get("prev_entry_sha256") != prev:
            problems.append(f"chain broken at {i}: seq {entry.get('seq')}, prev_entry_sha256 "
                            f"{entry.get('prev_entry_sha256')} does not follow entry {i - 1}")
        message = git(repo, "log", "-1", "--format=%s", commit).strip()
        if message != f"entry {i} receipt_root {entry.get('receipt_root')}":
            problems.append(f"commit {commit[:12]} message does not carry the entry's receipt_root")
        if receipts is not None:
            rpath = by_receipt_sha.get(entry.get("receipt_sha256"))
            if rpath is None:
                problems.append(f"{want}: no receipt file with sha256 {entry.get('receipt_sha256')}")
            else:
                from . import verify as verifier  # local import: log verification without Lean needs only hashing
                receipt = json.loads(rpath.read_bytes())
                if verifier.recompute_root(receipt) != entry["receipt_root"]:
                    problems.append(f"{want}: receipt {rpath.name} does not recompute to the logged receipt_root")
        prev = bk.sha256_bytes(blob)
    extra = [p.name for p in entries(repo)][len(commits) - 1:]
    if extra:
        problems.append(f"entry files not appended by a commit: {extra}")
    if pinned_head:
        ok = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", pinned_head, "HEAD"],
                            capture_output=True).returncode == 0
        if not ok:
            problems.append(f"published head {pinned_head[:12]} is not in the log history (history rewritten)")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcb log")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("repo", type=Path)
    p = sub.add_parser("append")
    p.add_argument("repo", type=Path)
    p.add_argument("receipt", type=Path)
    p = sub.add_parser("verify")
    p.add_argument("repo", type=Path)
    p.add_argument("--pinned-head", help="a previously published head commit that must still be in history")
    p.add_argument("--receipts", type=Path, action="append",
                   help="directory of receipt JSON files to re-derive each receipt_root, searched recursively (give "
                        "the intake's receipts/ for every task); may be repeated")
    args = ap.parse_args(argv)
    if args.cmd == "init":
        init(args.repo)
        print(git(args.repo, "rev-parse", "HEAD").strip())
        return 0
    if args.cmd == "append":
        if not (args.repo / ".git").exists():
            init(args.repo)
        try:
            entry = append(args.repo, args.receipt)
        except ValueError as err:
            print("REFUSED:", err)
            return 1
        print(json.dumps(entry, sort_keys=True))
        print("head", git(args.repo, "rev-parse", "HEAD").strip())
        return 0
    problems = verify(args.repo, args.pinned_head, args.receipts)
    print("LOG OK" if not problems else "LOG TAMPERED")
    for p in problems:
        print("  PROBLEM:", p)
    return 0 if not problems else 1


COMMANDS = {"log": ("append-only receipt log: init, append, verify", main)}


if __name__ == "__main__":
    sys.exit(main())
