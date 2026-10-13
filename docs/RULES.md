# Rules

This file sets out what a valid submission is, how it is scored, and how results are ranked and challenged. The
metric itself is defined in [METRIC.md](METRIC.md).

## The task

A task is a Lean library at a pinned toolchain and pinned dependencies. It has:
- **Editable modules.** You may rewrite these files freely: proofs, helper lemmas, tactics, metaprograms. Their
  declarations are charged.
- **Targets.** These are theorems in the editable modules, each named by its exact kernel name. Every target must
  still exist, with exactly the same statement.
- **Frozen definitions.** These are the definitions, instances and structures reachable from a target statement.
  They must keep the same meaning, so a target's meaning cannot be changed by redefining what it talks about.
  Meaning is compared without proofs. Inside a frozen definition and everything it reaches, every proof counts as
  the same placeholder, however it is written: a tactic block or a term, inline in the value or in an auxiliary
  `_proof_N` theorem that Lean generates. Lemmas used only as proofs are not part of the comparison. So you may
  rewrite any proof, including a proof argument or a proof field of a frozen definition, even when the new proof
  lets the kernel reduce the definition differently (kernel computation is free). Types, data and
  propositions are compared exactly: changing a function, a data field, or a proposition-valued field or definition
  (for example `n < 5` into `n < 6`) is rejected as `frozen definition changed` (`TASK.json`
  `frozen_derivation.proofs`).
- **Background.** Everything else in the import closure (for example Mathlib, or `Init` for the toy task) is free
  and read-only.

`make-task` turns `tasks/<id>/task_source.json` into a task package. The package's `TASK.json` pins the toolchain,
the fixed project files, the target statement hashes, the frozen closure hashes, the allowed axioms, the metric id
and the baseline. The package's sha256 identifies the task.

**The task specification is the instruction.** The package's
`TASK.md` is self-contained: the task, the objective, called the proof-term node count (a one-paragraph definition
and small examples), what may be
edited, what must not change, the allowed axioms, the one limit that decides validity (the kernel heartbeat limit
per declaration, `TASK.json` `limits.kernel_heartbeats`; wall-clock time, memory, processes and disk written
are safety limits that give no verdict), the commands, and how results are reported. `TASK.json` carries the same facts for programs, with the objective in its `objective` block. Neither
states a time limit, and neither names a recorded metric as an objective. An agent is given one line, such as "Read
TASK.md in this directory and do the task."; there is no other prompt.

## What is scored

The scored object is the **recompiled source → kernel export**. The verifier compiles your submitted source with the
organizer's toolchain, replays every editable module in the Lean kernel (`leanchecker`), and computes validity and
the score from the declarations it rebuilt. A bundle carries no compiled `.olean` files: the rebuilt kernel export
is scored, and it must equal the export the bundle carries.

Scores:
- **The proof-term node count** (technical name `expr_nodes_with_levels_v1`) is the primary metric, the ranking
  key and the only objective participants are given (in `TASK.md`, `TASK.json` `objective` and the `./check`
  output). It counts kernel `Expr` tree nodes, universe levels included. Targets pay for their proof value. Every
  other declaration in an editable module that the targets reach pays for type, value and recursor rules.
  Background declarations are free. Below, "node count" means this metric.
- **`dag_nodes_v1`** is recorded next to the node count for analysis. It counts the same terms with shared subterms counted once
  (see Sharing).
- `./check` prints the compression factor (baseline / node count), nodes remaining (the node count) and nodes removed
  (baseline − node count), where the baseline is the node count of the task's unmodified library;
  reduction = 100 × (baseline − node count) / baseline is also recorded. The leaderboard shows node counts and ranks
  only.

Changing any counting rule creates a new metric id. An existing id is never redefined.

Implementation status: `./check --json` and every receipt carry all three values in `scores`, computed by the
compiled counter `lcbench-metric` from the charged kernel export (docs/METRIC.md); the Python reader
`mcb export-recount` recomputes the node count and `dag_nodes_v1` from the same export.

## Tracks

**Modes.** A mode is part of the task package. It is never mixed within a ranking.

| mode | what the package contains | status |
|---|---|---|
| `warm` | The full original proofs, editable. This is the official track | official |
| `no_hint` | Every target proof replaced by `sorry`, with statements unchanged (make-task checks the statement hashes). The originals are not in the package | separate track, never ranked together with warm |

**Leaderboard tracks.** The leaderboard has three tracks, `open`, `agent-60` and `agent-120`; their ranking by average
rank over a task set and what a submission's `metadata.yaml` holds are in [SUBMITTING.md](SUBMITTING.md).

**The run's track.** Every checkout records what kind of run it is, and the leaderboard tracks are decided from that
record:

| run track (`mcb start --track`) | what it is | leaderboard tracks it counts on |
|---|---|---|
| `agent` | an agent working alone | `agent-60` when the checkout's budget is at most 3600 seconds, `agent-120` when it is at most 7200 seconds, and always `open` |
| `record` | anyone, by any means, including by hand | `open` only |
| none (no `--track`) | an undeclared run | `open` only |

`mcb start --track agent|record` writes the run track into the checkout's `RUN.json`, and `./submit` records it in
`SUBMISSION.json` (`metadata.track`); `./submit --track` may state it too but may not contradict `RUN.json`. The
receipt chains the track; a bundle that declares no track has `track: null`. Bundles may carry a hashed trajectory
(`./submit --log DIR`).

## Budgets

A time or compute budget is a property of a **checkout**, not of the task or of any prompt:

- `mcb start <package> --budget-seconds N` (or `--unlimited`, the default) writes `RUN.json` into the
  checkout: `budget_seconds` (a positive whole number of seconds, or `"unlimited"`), `track`, `started_at` (UTC) and
  `scorer_version`, with the task's name and `TASK.json` sha256. `TASK.md` tells the participant that `RUN.json`
  states the budget.
- The budget is enforced by whoever runs the participant, never by the task: `./check` and `./submit` do not stop
  anyone. Whoever runs an agent stops it at the deadline from outside.
- The budget covers the agent's work only. A final `./submit` run on the agent's files after the agent stops, and any
  later `mcb verify`, are not counted.
- An agent may run `./submit` itself, several times. The run's result is the bundle you choose to submit; with a
  final `./submit` after the agent stops, it is that bundle. An unlimited run may still be stopped by an outside
  safety limit (for example a machine's job time limit); `RUN.json` still says unlimited.
- `./submit` records in `SUBMISSION.json` `metadata.run` the budget, the start time, the wall time elapsed since
  the start, the sha256 of `RUN.json` and the token usage declared with `--tokens-json FILE` (a JSON object).
- The verifier copies this into the receipt's `run` block with its budget class (`"3600s"`, or `"unlimited"`). A
  bundle without run metadata declared no budget and is in the unlimited class. The block is outside
  `receipt_root`, because `bundle_sha256`, which is chained, already binds `SUBMISSION.json`; a budget that is not
  a positive integer or `"unlimited"` rejects the bundle.
- Results are compared only within one budget class, and there is always an unlimited class.
- A new checkout does not start a new budget: if you set up the task again, the time spent in earlier checkouts
  counts. `./submit` records only the time since the latest `start`, so state the total from your first `start` in
  the pull request.

## Validity

A submission is valid only if every rule below holds. `./check` runs the same code as the official `mcb verify`.
The only differences are the sandbox and the signature, so a local VALID means the verifier will also say VALID.

| rule | how it is checked | status |
|---|---|---|
| Same toolchain | `lean --githash` must equal the pinned githash. Every `LAKE_*`, `LEAN_*` and `ELAN_*` variable is cleared, and `lean`, `lake` and `leanchecker` are called by absolute path from one resolved toolchain (a script shim is refused) | implemented |
| Fixed project files | `lakefile`, `lake-manifest.json` and `lean-toolchain` are hash-checked. Pinned package revisions are checked | implemented |
| Package from this scorer | the package's shipped tooling (`benchkit.py`, the organizer Lean tool sources in `.benchmark/tools/`) must equal the verifier's; otherwise `package tooling differs from this verifier` names both scorer versions. `TASK.json` records the `scorer_version` that built the package | implemented |
| Background integrity | `TASK.json` pins `background.olean_sha256_root`, the sha256 of the package's `.benchmark/background.sha256`, which lists a digest of the `.olean` and `.olean.server` files (not `.olean.private`, which is not byte-reproducible) of every background module of the original's import closure and of every toolchain module. Right after `lake build`, before kernel replay, every listed module is re-hashed (`background hash mismatch`); an import of a background module outside the list is `background module not pinned` | implemented |
| Only allowed files in a bundle | sources of the editable files, the charged kernel export, `SUBMISSION.json`, `MANIFEST.sha256` and an optional `trajectory/` | implemented |
| Track | `metadata.track` is `agent`, `record` or absent; anything else is `unknown track` | implemented |
| Compiles | `lake build` with the pinned toolchain at the canonical build path `/private/tmp/lcbench-build/<task sha12>/project`, inside the bubblewrap sandbox for official verification (`verify --sandbox bwrap`) | implemented |
| Kernel replay | every charged module is replayed through the Lean kernel by `lcbench-check replay`: stock `leanchecker`'s check (`Environment.replay` of the module's constants on its imports), with the background imported once and the owners replayed in import order, so an owner another owner imports is re-checked rather than trusted. When sharing one environment could accept something `leanchecker` rejects (a constant outside the owner's import closure, a name clash, a background module importing an owner), every owner is replayed exactly as `leanchecker` does. `--paranoid` (or `LCBENCH_PARANOID=1`) also runs stock `leanchecker` on every owner; a disagreement rejects. Each declaration is checked on a fresh thread with the kernel's own heartbeat limit, `limits.kernel_heartbeats` per declaration, and the heartbeats it used are recorded. A declaration that fails the kernel, or that uses more heartbeats than the limit, rejects the bundle | implemented |
| Targets present, same statement | exact structural hash of each target's kernel type, including binder names and types, binder info and universe parameters | implemented |
| Frozen definitions unchanged | kernel closure hash of each frozen definition: the types, data values and propositions of the declarations it reaches, with every proof replaced by one placeholder; constants used only inside proofs are not reached (see The task) | implemented |
| Axioms | every target may depend only on the allowed axioms (see the line below). `sorryAx`, `Lean.ofReduceBool` and `Lean.trustCompiler` reject | implemented |
| Imports | an editable module may import the other editable modules, the fixed scored modules (`fixed_charged`, if the task has any) and pinned background modules (see Background integrity); any other import is rejected (`import X not allowed in F`) | implemented |
| Rebuilt export equals submitted | the recompiled kernel export must equal the bundled one | implemented |
| Resource limits | **Only a deterministic limit decides validity**: `limits.kernel_heartbeats` from `TASK.json`, the most heartbeats (Lean's count of small allocations, the same on every machine) the kernel may use to check one declaration during kernel replay. A declaration over it is INVALID with a reason such as `limit: kernel_heartbeats exceeded (10000000000 heartbeats per declaration) by Foo.bar`. Elaboration is bounded per declaration by Lean's own `maxHeartbeats`, as the task's and the participant's source set it: Lean has no organizer setting that the source cannot change, and the lcbench-v1 tasks' own sources use `set_option maxHeartbeats 0`, so a non-terminating elaboration is stopped only by the wall-clock safety cap below. **Wall-clock time and memory never decide validity.** They, the number of processes and the disk written are safety limits that protect the machine: `limits.safety_wall_s` (default 6 h for the whole evaluation), `limits.mem_mb`, `limits.max_processes` (default 512 processes in a command's process tree) and `limits.disk_mb` (default 16 GB written to the build directory). Reaching one stops the whole process tree and gives **no verdict**: `./check` and `./submit` print `NO VERDICT` and exit 3, `mcb verify` writes verdict `ERROR` with status `infrastructure_error` and exits 3, such a receipt is never logged, and the intake re-runs the submission on another machine. The same holds when the organizer's own tools fail to build on the machine (`infrastructure error`). Every toolchain command runs under a watchdog that samples its process tree at most 0.5 s apart and sooner as the tree nears a cap, and stops it when its memory passes 90% of `mem_mb`: each sample comes before a tree growing at 32 GB/s could pass that stop point (three times what one process allocated on the fastest machine measured), never less than 5 ms after the previous one, so a tree that allocates at most 32 GB/s is stopped below `mem_mb` (for every task's `mem_mb`, 4 GB or more); a faster one can pass `mem_mb` by what it allocates in one interval. When a command ends, whatever its exit status, the run also stops without a verdict if one of its processes had a resident set past that point (the kernel's high-water mark), if it left more than `max_processes` processes running, or if the build directory holds at least `disk_mb`; no single file can grow past `disk_mb` (`RLIMIT_FSIZE`). Because the other caps are sampled, a fast burst can still pass them before the stop: in the probes a fork burst started up to about 450 processes past `max_processes`, and on an overloaded node an earlier, slower-sampling version let a fast allocation reach 1.3 GB past `mem_mb`. The memory of a tree is the private memory of each process (anonymous and shared-memory pages on Linux, `phys_footprint` on macOS) plus the largest file-backed resident set in the tree, counted once (parallel Lean processes map the same `.olean` files). On Linux every command runs below a small subreaper process, so a process that double-forks or starts its own session stays in the watched tree and is stopped with it. Only the last 16 MB of each command's output are kept. Every receipt records the kernel heartbeats (`environment.heartbeats`), the wall time (`environment.wall_s`, `environment.timings_s`) and the peaks (`peak_rss_mb`, `peak_processes`, `peak_disk_mb`). **macOS `./check` (no sandbox):** a process that double-forks into its own session leaves the watched tree, so memory, process and disk limits do not cover it; official verification runs on Linux in the sandbox, where the pid namespace keeps every process in the tree | implemented |
| Forbidden text | `sorry`, `native_decide`, `implemented_by` and similar words, when the task lists them. This is a lint only; the kernel and axiom checks are the real gate | implemented as listed per task |

Allowed axioms: `propext`, `Quot.sound`, `Classical.choice`.

Several things are allowed: any change to proofs, new helper lemmas and definitions in editable modules, new tactics,
macros and elaborators, metaprograms that construct proof terms, and removing unused declarations that are not
targets or frozen definitions.

## Sharing

Sharing is allowed. A proof may bind a repeated subterm once with `let`, factor it into an auxiliary
declaration, or generate the term with a metaprogram that shares subterms. All of these are ordinary Lean and
produce valid kernel terms.

The node count charges **tree size**. A `let`-bound value is counted once where it is bound, and every use of the variable counts
as one node. An auxiliary declaration in an editable module pays its own type and value once, and every reference to
it counts as one node. A metaprogram is charged for the term it produces, not for its source text.

Every receipt also records `dag_nodes_v1`, the size of the same charged terms with identical subterms counted once.
Readers can then see how much of a reduction comes from sharing. For each task of `lcbench-v1`, the results page of
the MathCompressionBench site shows the mechanical-sharing floor: the node count of the task's original proofs after an automatic
pass that binds every repeated subterm once (a common-subexpression pass). A submission that only matches that floor
has added no mathematical content.

Sharing metaprograms have produced some of the largest reductions. They stay valid under v1. A
sharing-invariant ranking metric is a question for a later version.

## Submitting

`./submit` re-checks and writes `submissions/submission-<first 16 hex digits of the ID>.tar.gz`, where the submission ID is the sha256 of the
deterministic tarball. The bundle carries the source and a self-reported score. The verifier recomputes everything and
rejects the bundle if the self-report differs. Submissions are made by pull request to the submissions repository
https://github.com/MathCompressionBench/mcb-submissions, whose README gives the steps and explains
what self-reported, verified and mismatch mean ([SUBMITTING.md](SUBMITTING.md) is the full specification). `./submit` sends nothing itself; it prints where to submit.

## Receipts, the log and the challenge window

The verifier writes a receipt (`lcbench.receipt.v1`). Its `receipt_root` is a hash chain over task, bundle, source,
export, scores, verdict and verifier (`receipt.py` lists each value). The trajectory, the
`status` and the `environment` block (OS, sandbox, timings, memory) are outside the root, so the
same bundle has the same root on every machine. Only the organizer's signature vouches for `status: verified`. The
receipt root is appended to an append-only log (`mcb log`), and rejected submissions are logged too.
`mcb verify-receipt` re-derives the root and checks the signature, and `mcb verify` rebuilds and re-scores the
bundle.

Challenge window:
- A record-track entry stays **provisional** for 14 days after its receipt is logged.
- During that window anyone may challenge it. A challenge is a re-run of `mcb verify` on the published task
  package and bundle that gives a different verdict or score, or a demonstration that a rule above was broken.
- After the window the entry becomes **verified**, or **withdrawn** with a stated reason.
- Receipts are never deleted, and a withdrawn entry keeps its log entry.

Ties: on each task, systems with the same node count share the mean of the places they span, and systems with the
same average rank share a position ([SUBMITTING.md](SUBMITTING.md), Ranking). Byte-identical bundles count once: the
pull request opened first gets the credit, and a later one is listed as a duplicate of it and is not ranked.
