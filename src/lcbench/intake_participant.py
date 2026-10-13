# ABOUTME: `lcbench intake add`, the participant side of the intake: checks a bundle against the submissions checkout's
# ABOUTME: accepted-packages.json and copies it there with its <id>.json (the organizers' check repeats these checks).
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import submission_meta
from .runtime import benchkit as bk

META_SCHEMA = "lcbench.intake_submission.v1"
PRIMARY = "expr_nodes_with_levels_v1"
BUNDLE_SUBMISSION_JSON = "submission/SUBMISSION.json"


ACCEPTED_PACKAGES_FILE = "accepted-packages.json"


def bundle_submission(bundle: Path) -> dict | None:
    """The bundle's SUBMISSION.json, or None when it has no readable one."""
    import tarfile
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            sub = json.loads(tar.extractfile(BUNDLE_SUBMISSION_JSON).read())
    except (tarfile.TarError, KeyError, OSError, EOFError, ValueError, AttributeError):
        return None
    return sub if isinstance(sub, dict) else None


def bundle_track(sub: dict) -> str | None:
    """The track the bundle records (SUBMISSION.json metadata.track, which `lcbench start --track` sets)."""
    meta = sub.get("metadata")
    return meta.get("track") if isinstance(meta, dict) else None


def add_problems(checkout: Path, task: str, bundle: Path, track: str | None = None) -> tuple[list[str], list[str]]:
    """(problems, notes) of adding `bundle` as a `task` submission: the bundle must be made for that task, a given
    `track` must be the one the bundle records; and when the submissions checkout has accepted-packages.json, the task
    must be open and the bundle's package accepted (the same words as the repository's scripts/check_submission.py)."""
    sub = bundle_submission(bundle)
    if sub is None:
        return [f"{bundle} has no readable {BUNDLE_SUBMISSION_JSON} (use the file ./submit wrote)"], []
    problems = []
    if sub.get("task_name") != task:
        problems.append(f"the bundle was made for the task {sub.get('task_name')!r}, not {task!r}")
    if track is not None and track != bundle_track(sub):
        problems.append(f"--track {track} differs from the track the bundle records ({bundle_track(sub) or 'none'}); "
                        f"the track is the one `mcb start --track` gave the workspace")
    listing = checkout / ACCEPTED_PACKAGES_FILE
    if not listing.is_file():
        return problems, [f"{listing} does not exist, so whether {task!r} is open was not checked"]
    accepted = json.loads(listing.read_text())["tasks"]
    if task not in accepted:
        problems.append(f"the organizers do not accept the task {task!r} (open tasks: {', '.join(sorted(accepted))})")
    elif sub.get("task_sha256") not in {p["task_sha256"] for p in accepted[task]}:
        listed = "; ".join(f"package sha256 {p['package_sha256']} (task sha256 {p['task_sha256']}"
                           + (f", get it with: {p['get']}" if p.get("get") else "") + ")" for p in accepted[task])
        problems.append(f"the bundle was made from a {task} package the organizers do not accept (task sha256 "
                        f"{sub.get('task_sha256')}, as ./check prints it). Accepted {task} packages: {listed}. Start a "
                        f"new workspace from an accepted package, copy your proof files into it and run ./submit again")
    return problems, []


def add_submission(checkout: Path, task: str, bundle: Path, participant: str, self_receipt: Path | None,
                   claimed_a: int | None = None, track: str | None = None, entry: str | None = None) -> str:
    """Participant side: copy a bundle (and optionally a self-reported receipt) into a submissions checkout with its
    metadata file. The claimed score and the track come from the bundle's SUBMISSION.json unless given. `entry` names
    the submission's entries/<entry>/metadata.yaml that the bundle belongs to."""
    sid = bk.sha256_file(bundle)
    dest = checkout / "submissions" / task
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundle, dest / f"{sid}.tar.gz")
    sub = bundle_submission(bundle) or {}
    if claimed_a is None:
        claimed_a = sub["claimed_scores"][PRIMARY]
    if track is None:
        track = bundle_track(sub)
    meta = {"schema": META_SCHEMA, "task": task, "submission_id": sid, "participant": participant,
            "track": track, "claimed": {PRIMARY: claimed_a}, **({"entry": entry} if entry is not None else {})}
    (dest / f"{sid}.json").write_text(bk.pretty_json(meta))
    if self_receipt is not None:
        shutil.copyfile(self_receipt, dest / f"{sid}.receipt.json")
    return sid


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("checkout", type=Path)
    p.add_argument("task")
    p.add_argument("bundle", type=Path)
    p.add_argument("--participant", required=True)
    p.add_argument("--self-receipt", type=Path)
    p.add_argument("--claimed", type=int, help="self-reported proof-term node count (default: the bundle's own claim)")
    p.add_argument("--track", choices=list(bk.TRACKS),
                   help="optional: the track the bundle records (from `mcb start --track`); refused if it differs")
    p.add_argument("--entry", help="the submission's entry name: the bundle belongs to entries/<entry>/metadata.yaml")


def add(args) -> int:
    problems, notes = add_problems(args.checkout, args.task, args.bundle, args.track)
    if args.entry is not None and not submission_meta.ENTRY_NAME.fullmatch(args.entry):
        problems.append(f"--entry {args.entry!r}: use lowercase letters, digits, '.', '_' or '-' (at most 64)")
    for note in notes:
        print(f"note: {note}")
    if problems:
        print("NOT ADDED: the organizers would refuse this submission:")
        for p in problems:
            print(f"- {p}")
        return 1
    sid = add_submission(args.checkout, args.task, args.bundle, args.participant, args.self_receipt,
                         args.claimed, args.track, args.entry)
    copied = f"{sid}.json and {sid}.receipt.json (your self-receipt)" if args.self_receipt is not None \
        else f"{sid}.json"
    print(f"added submissions/{args.task}/{sid}.tar.gz, {copied}; commit them on a new branch and open a pull request")
    if args.entry is not None and not (args.checkout / "entries" / args.entry / "metadata.yaml").is_file():
        print(f"note: entries/{args.entry}/metadata.yaml does not exist yet; copy templates/metadata.yaml there "
              f"and fill it in")
    return 0


def main(argv=None) -> int:
    """`lcbench intake add` alone: the command a participant's checkout has when it holds no organizer intake."""
    ap = argparse.ArgumentParser(prog="mcb intake")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_arguments(sub.add_parser("add", help="put a bundle and its metadata into a submissions checkout"))
    return add(ap.parse_args(argv))


# registered only where lcbench.intake (which registers `intake` with every subcommand) is absent
COMMANDS = {"intake": ("participant: add a bundle to a submissions checkout (add)", main)}


if __name__ == "__main__":
    sys.exit(main())
