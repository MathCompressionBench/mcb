# ABOUTME: lcbench.receipt.v1: the hash chain task -> bundle -> source -> export -> scores -> verdict -> verifier.
# ABOUTME: The environment block (OS, sandbox, timings) and the status are reported outside receipt_root.
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .runtime import benchkit as bk

RECEIPT_SCHEMA = "lcbench.receipt.v1"
RELEASE = "lcbench-v1"
SIGNATURE_NAMESPACE = "lcbench-receipt"
CHAIN_LABELS = ["task", "bundle", "source", "export", "scores", "verdict", "verifier"]
# Receipt fields outside the chain: they may differ between machines and runs without changing receipt_root. `run`
# (budget class, elapsed time, tokens) is copied from the bundle's SUBMISSION.json, which bundle_sha256 already binds.
UNCHAINED = ("environment", "trajectory", "run", "status", "lints", "log_tail", "hash_chain",
             "receipt_root")


def hash_chain(links: list[tuple[str, str]]) -> tuple[list[dict], str]:
    h = bk.sha256_bytes(RECEIPT_SCHEMA.encode())
    chain = []
    for label, value in links:
        h = bk.sha256_bytes(f"{h}|{label}|{value}".encode())
        chain.append({"label": label, "value": value, "chained": h})
    return chain, h


def digest(obj) -> str:
    return bk.sha256_bytes(bk.canonical_json(obj))


def chain_links(r: dict) -> list[tuple[str, str]]:
    """The chained values, each derived only from receipt fields inside the root."""
    context = {"verifier": r["verifier"], "release": r["release"], "set": r["set"],
               "task_revision": r["task_revision"], "track": r["track"], "metric_ids": r["metric_ids"],
               "toolchain": r["toolchain"], "mathlib_rev": r["mathlib_rev"]}
    return [("task", r["task_sha256"]), ("bundle", r["bundle_sha256"]),
            ("source", r["source_tree_sha256"] or "none"), ("export", r["kernel_export_sha256"] or "none"),
            ("scores", digest({"score": r["score"], "scores": r["scores"]})),
            ("verdict", digest({"verdict": r["verdict"], "problems": r["problems"]})),
            ("verifier", digest(context))]


def seal(receipt: dict) -> dict:
    """Add hash_chain and receipt_root to a receipt whose chained fields are filled in."""
    chain, root = hash_chain(chain_links(receipt))
    receipt.update(hash_chain=chain, receipt_root=root)
    return receipt


def recompute_root(receipt: dict) -> str:
    return hash_chain(chain_links(receipt))[1]


def status(verdict: str, sandbox: str) -> str:
    """Only an accepted bundle verified inside a sandbox is `verified`; `--sandbox none` runs are `unverified`. An
    `ERROR` verdict (a safety limit stopped the run: wall clock, memory, processes or disk) is `infrastructure_error`:
    no verdict, re-run it."""
    if verdict == "ERROR":
        return "infrastructure_error"
    if verdict != "ACCEPT":
        return "rejected"
    return "unverified" if sandbox == "none" else "verified"


def sign(path: Path, key: Path) -> Path:
    """OpenSSH signature (ed25519, deterministic) of the receipt file, which contains receipt_root; runs in its own
    process after every participant-code step has finished. Verify with ssh-keygen -Y verify and allowed_signers."""
    sig = path.with_name(path.name + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", SIGNATURE_NAMESPACE, str(path)],
                   check=True, capture_output=True)
    return sig


def load(path: Path) -> dict:
    return json.loads(path.read_bytes())
