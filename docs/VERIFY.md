# Verifying results yourself

Every submission checked by the organizers comes with three things:
- a task package, identified by its tarball sha256;
- a submission bundle, identified by its sha256, which is also the submission ID;
- a receipt: the organizers' record of the check, which they post on the submission's pull request.

Anyone can re-derive a receipt from the package and the bundle. This page shows how, from the cheapest check to the
most expensive.

## 1. Check a receipt's hash chain (no Lean needed)

A receipt's `receipt_root` (schema `lcbench.receipt.v1`) is a sha256 chain over these fields, in order:
1. `task` (the `TASK.json` sha256)
2. `bundle` (the submission ID)
3. `source` (the tree hash of the submitted sources)
4. `export` (the sha256 of the charged kernel export, `lcbench.metric_export.v1`, that the scores come from)
5. `scores` (the sha256 of the canonical `score` and `scores` JSON: the proof-term node count and `dag_nodes_v1`)
6. `verdict` (the verdict and its problems)
7. `verifier` (the verifier block with `verifier_sha256` and commit, release, set, task revision, track, metric
   ids, toolchain and Mathlib revision)

Each step is `h_i = sha256(h_{i-1}|label|value)`, starting from `sha256("lcbench.receipt.v1")`. The trajectory,
the `run` block (budget class, elapsed time and declared tokens, copied from the bundle's `SUBMISSION.json`, which the chained bundle hash already binds), `status` and the `environment` block (operating
system, sandbox, timings, peak memory) are reported outside the root, so a macOS run without a sandbox and an official
Linux run of the same bundle have the same root. The verifier commit is part of the root: from a git checkout it is
`git rev-parse HEAD`, and from a source archive made with `git archive` it is the commit written into
`src/lcbench/_commit`.

Changing any one of them changes the root. From the repository root, this recomputes the root from the receipt's
own fields and compares (replace `receipt.json` with the receipt's path):

<!-- receipt-root:begin -->
```sh
PYTHONPATH=src python3 -c '
import json, sys
from lcbench.verify import recompute_root
r = json.load(open(sys.argv[1]))
print("receipt_root OK" if recompute_root(r) == r["receipt_root"] else "receipt_root MISMATCH")
' receipt.json
```
<!-- receipt-root:end -->

`mcb verify-receipt RECEIPT --bundle BUNDLE` recomputes the root too, and also checks the signature (with
`--allowed-signers`) and recounts the node count from the bundle's stored export in Python. It checks consistency,
not validity: it never builds or replays anything. Because `status` is outside the root, a receipt that says
`verified` passes `verify-receipt` only together with a good signature.

The verifier commit may be one that you cannot look up. `verifier_sha256`, in the same block, names the verifier's code
itself: the sha256 over `verify.py`, `receipt.py`, `runtime/benchkit.py` and the Lean tool sources. A checkout with the
same code prints the same value:

```sh
PYTHONPATH=src python3 -c 'from lcbench.verify import verifier_sha256; print(verifier_sha256())'
```

## 2. Check the signature

The organizers sign receipts with an ed25519 key through OpenSSH (namespace `lcbench-receipt`). The signature sits
next to the receipt as `receipt.json.sig`, and the public key is in [allowed_signers](allowed_signers) under the
principal `organizer@lcbench`:

```sh
ssh-keygen -Y verify -f docs/allowed_signers -I organizer@lcbench -n lcbench-receipt \
  -s receipt.json.sig < receipt.json
bin/mcb verify-receipt receipt.json --allowed-signers docs/allowed_signers
```

## 3. Re-verify the bundle

This step rebuilds the source and re-scores it. It needs the task package, the bundle, the Lean toolchain named in
the package's `TASK.json`, and the pinned Lake packages for tasks with dependencies (`--deps`, described in the
README section "The lcbench-v1 tasks: dependencies"):

```sh
bin/mcb verify <task package .tar.gz> <submission-....tar.gz> --workdir <empty dir> --out my-receipt.json \
  --expected-id <submission ID> [--deps <pinned Lake packages>]
bin/mcb verify-receipt --compare my-receipt.json <the organizers' receipt>
```

`verify` does the following:
- checks the package and bundle manifests and the bundle file allow-list;
- recompiles the submitted sources with the pinned toolchain;
- replays every editable module in the kernel, with at most `limits.kernel_heartbeats` heartbeats per declaration;
- checks target statements, frozen definitions and axioms;
- recounts the node count and compares every self-reported hash and score.

Two verifications of the same task package and bundle never give byte-identical receipts: the `environment`
block (operating system, timings, peak memory, processes and disk, sandbox; the kernel heartbeats there are the same
on every machine) and `status` (`verified` only in the official sandbox) describe the run.

A run that a safety limit stops (the wall-clock cap `limits.safety_wall_s`, memory `limits.mem_mb`, the number of
processes `limits.max_processes`, the disk written `limits.disk_mb`) has no verdict: the receipt says verdict
`ERROR`, status `infrastructure_error` and one problem that names the limit, `mcb verify` exits 3, and
`mcb log append` refuses the receipt. The organizers' own tools failing to build on the machine is handled the
same way (`infrastructure error`). Run it again on another machine. Everything else, `receipt_root` included, is
deterministic on macOS and Linux. `verify-receipt --compare` prints `SAME RESULT` when the two receipts are equal
outside `environment`, `status` and `hash_chain`, and otherwise names the differing fields. A difference in
`receipt_root` with your verifier at the receipt's `verifier.commit` means you have found a problem. Report it as a
challenge (see [RULES.md](RULES.md)).

Official verification runs in a sandbox on Linux. A verification on macOS uses no sandbox, and it reproduces the
numbers but is not itself an official verification.

## 4. Check the log

The log is a git repository with one commit per entry. Each entry commits one `receipt_root` and chains to the
previous entry by sha256:

```sh
bin/mcb log verify <log repo> --receipts <directory of receipts> [--pinned-head <a head commit you saw earlier>]
```

It prints `LOG OK` only in these cases:
- every commit after the first adds exactly the next entry file and changes nothing else;
- the entries chain;
- each commit message carries its entry's `receipt_root`;
- with `--receipts`, every logged root recomputes from a receipt file;
- with `--pinned-head`, a head you recorded earlier is still in the history.

## 5. The checking tools inside a task package

Every task package carries the organizers' Lean tools in `.benchmark/tools/` (the same files as `lean/` here). They
build two executables, which `./check`, `./submit` and `mcb verify` run after the build:

- `lcbench-metric REQUEST.json` computes the metrics. Its request and result formats are in
  [METRIC.md](METRIC.md), section 5.
- `lcbench-check replay OWNER_MODULE...` replays every editable module in the kernel (see the Kernel replay rule in
  [RULES.md](RULES.md)), and `lcbench-check facts REQUEST.json` imports the editable modules once and writes both the
  facts the verifier checks (target statement hashes, frozen definition hashes, axioms) and the metric result. Its
  request is the `lcbench-metric` request plus `"extract": {"roots": [target names], "frozen": [frozen definition
  names], "out": "<facts file>"}` and an optional `"result"` file for the metric result.

## What a receipt does not show

A valid receipt shows that the recompiled source proves the unchanged targets within the rules, with the stated node
count. It does not show that the proof is readable or how much of it is shared: sharing is allowed, and
`dag_nodes_v1` shows how much of it there is. It also does not show how the run that produced the source was made. For the agent
tracks, that is what the run's recorded track and budget and the declared metadata are for (see
[SUBMITTING.md](SUBMITTING.md)).
