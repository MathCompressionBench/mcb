# ABOUTME: `lcbench verify-receipt`: re-derives a receipt's hash chain, checks its ssh signature and re-scores the
# ABOUTME: bundle's stored kernel export without any Lean build; fails if the export or any chained field changed.
from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from pathlib import Path

from . import receipt as rc
from .runtime import benchkit as bk


def export_total(export: bytes) -> tuple[int, str]:
    """A from the stored export. With the metric area's `lcbench.export_reader` the node counts are recounted
    from the serialized kernel terms; without it the per-declaration costs in the export are summed."""
    try:
        reader = importlib.import_module("lcbench.export_reader")
    except ImportError:
        reader = None
    header = json.loads(export.split(b"\n", 1)[0] or b"{}")
    if header.get("schema") == "lcbench.metric_export.v1" and reader is not None \
            and hasattr(reader, "metric_from_export"):
        return reader.metric_from_export(export)["expr_nodes_with_levels_v1"], "recounted by lcbench.export_reader"
    rows = [json.loads(line) for line in export.splitlines()[1:] if line.strip()]
    return sum(r["cost"] for r in rows), "sum of the export's per-declaration costs"


def check(receipt_path: Path, bundle: Path | None, allowed_signers: Path | None, signer: str | None,
          notes: list[str] | None = None) -> list[str]:
    """Problems with a receipt: its chain, its signature (with allowed_signers), and its bundle's stored export.
    This checks consistency, not validity: only `lcbench verify` rebuilds the source and replays the kernel.
    `status` and `environment.sandbox` are outside receipt_root, so a receipt that claims `verified` is accepted
    only with a good organizer signature. `notes` collects what was and was not checked."""
    notes = [] if notes is None else notes
    problems = []
    r = json.loads(receipt_path.read_bytes())
    chain, root = rc.hash_chain(rc.chain_links(r))
    if root != r.get("receipt_root") or chain != r.get("hash_chain"):
        problems.append(f"hash chain does not re-derive: computed root {root}, receipt says {r.get('receipt_root')}")
    sig = receipt_path.with_name(receipt_path.name + ".sig")
    signed = False
    if allowed_signers is not None:
        if not sig.is_file():
            problems.append(f"no signature file {sig.name}")
        else:
            with open(receipt_path, "rb") as f:
                out = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", signer or "",
                                      "-n", rc.SIGNATURE_NAMESPACE, "-s", str(sig)], stdin=f, capture_output=True,
                                     text=True)
            if out.returncode != 0:
                problems.append(f"signature does not verify: {out.stderr.strip() or out.stdout.strip()}")
            else:
                signed = True
                notes.append(f"signature ok ({signer})")
    if not signed:
        notes.append("unsigned check: the chain and export are self-consistent, but nothing vouches for status, "
                     "sandbox or validity (pass --allowed-signers docs/allowed_signers to check the signature)")
        if r.get("status") == "verified":
            problems.append("status 'verified' is not vouched for: status and sandbox are outside receipt_root, "
                            "so a verified receipt needs its organizer signature (--allowed-signers)")
    if bundle is not None:
        if bk.sha256_file(bundle) != r["bundle_sha256"]:
            problems.append("bundle sha256 differs from the receipt's bundle_sha256")
        export = read_export(bundle)
        if export is None:
            problems.append("bundle has no kernel_export.jsonl or score_breakdown.jsonl")
        elif bk.sha256_bytes(export) != r["kernel_export_sha256"]:
            problems.append("stored kernel export differs from the receipt's kernel_export_sha256")
        else:
            total, how = export_total(export)
            if total != (r.get("scores") or {}).get("expr_nodes_with_levels_v1"):
                problems.append(f"export gives a node count of {total} ({how}); receipt says {r['scores']}")
    return problems


def read_export(bundle: Path) -> bytes | None:
    import tarfile
    with tarfile.open(bundle, "r:gz") as tar:
        for name in (f"submission/{n}" for n in bk.BUNDLE_EXPORT_FILES):
            try:
                return tar.extractfile(name).read()
            except KeyError:
                continue
    return None


# Receipt fields that legitimately differ between two verifications of the same (task, bundle): where and how
# the verifier ran. Everything else, receipt_root included, must be equal.
RUN_SPECIFIC = ("environment", "status", "hash_chain")


def compare(a_path: Path, b_path: Path) -> list[str]:
    """Top-level receipt fields, other than RUN_SPECIFIC, whose values differ between two receipts."""
    a, b = json.loads(a_path.read_bytes()), json.loads(b_path.read_bytes())
    return sorted(k for k in set(a) | set(b) if k not in RUN_SPECIFIC and a.get(k) != b.get(k))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcbench verify-receipt")
    ap.add_argument("receipt", type=Path, nargs="+",
                    help="receipt JSON (two receipts with --compare)")
    ap.add_argument("--bundle", type=Path, help="the submission bundle, to re-score its stored kernel export")
    ap.add_argument("--allowed-signers", type=Path, help="ssh allowed_signers file (checks <receipt>.sig)")
    ap.add_argument("--signer", default="organizer@lcbench", help="signer identity in allowed_signers")
    ap.add_argument("--compare", action="store_true",
                    help="compare two receipts of the same bundle: equal outside environment and status")
    args = ap.parse_args(argv)
    if args.compare:
        if len(args.receipt) != 2:
            ap.error("--compare takes exactly two receipts")
        differing = compare(*args.receipt)
        if differing:
            print("DIFFERENT: " + ", ".join(differing))
            return 1
        print("SAME RESULT: equal outside " + ", ".join(RUN_SPECIFIC))
        return 0
    if len(args.receipt) != 1:
        ap.error("give one receipt (or two with --compare)")
    notes: list[str] = []
    problems = check(args.receipt[0], args.bundle, args.allowed_signers, args.signer, notes)
    print("RECEIPT OK" if not problems else "RECEIPT INVALID")
    for n in notes:
        print("  NOTE:", n)
    for p in problems:
        print("  PROBLEM:", p)
    return 0 if not problems else 1


COMMANDS = {"verify-receipt": ("re-derive a receipt's chain and signature and re-score the bundle's stored export "
                               "(no Lean build)", main)}


if __name__ == "__main__":
    sys.exit(main())
