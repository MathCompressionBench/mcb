# MathCompressionBench

MathCompressionBench is a Lean proof-compression benchmark. Each task is an existing Lean library with a fixed list of target
theorems. Your job is to make the kernel proof terms of those targets as small as possible. Target statements must
stay exactly as they are, and frozen definitions must keep their meaning: you may rewrite the proofs inside them, but
not their types, data or propositions.

The objective is the **proof-term node count**: the number of nodes in the kernel proof terms the targets need,
universe levels included, charged over the task's editable modules (technical name `expr_nodes_with_levels_v1`).
Smaller is better, and it is the only objective. Receipts also record `dag_nodes_v1` for analysis; it is not an
objective. The organizers rebuild your source, so the scored object is the recompiled source → kernel export. A
bundle holds your source files and that export, no compiled files. Rules: [docs/RULES.md](docs/RULES.md).
Metric: [docs/METRIC.md](docs/METRIC.md).

The task set `lcbench-v1` ([sets/lcbench-v1.json](sets/lcbench-v1.json)) has seven tasks: ramsey96, slt26, slt49,
permanent2, compactness5, mathlib197 and mathlib372 (`tasks/<id>/CARD.md` describes each). There is also a small toy
task, `toyarith`, for trying the tools.

The docs call "the study" the organizers' runs of six agents on the seven tasks of lcbench-v1 (September 2026); its
runs are listed on the leaderboards with the merged submissions ([docs/SUBMITTING.md](docs/SUBMITTING.md)).

## Submitting

Submissions are pull requests to https://github.com/MathCompressionBench/mcb-submissions; its README and
[docs/SUBMITTING.md](docs/SUBMITTING.md) have the steps. Open for submissions now: `toyarith-warm` and the seven
lcbench-v1 tasks, each as its warm and its no_hint package. The "lcbench check status" comment on your pull request is
posted and edited by the organizers' GitHub account only.

Get the tools with `git clone https://github.com/MathCompressionBench/mcb.git` (no GitHub account needed).

The submissions README names the accepted packages: each one's package sha256 and the GitHub release of this
repository that holds it (the lcbench-v1 packages are the assets of the release that `sets/lcbench-v1.json` names).
Every release asset downloads without a login from
`https://github.com/MathCompressionBench/mcb/releases/download/<release>/<file>`. For the toy package:

- download it: `curl -fLO https://github.com/MathCompressionBench/mcb/releases/download/<release named there>/toyarith-warm.tar.gz`
  and the same URL with `.sha256` for its `.sha256` file (or `gh release download <release> --repo MathCompressionBench/mcb`
  when you are logged in to GitHub); then `bin/mcb start "file://$PWD/toyarith-warm.tar.gz" --dir <workspace>
  --budget-seconds <N> --track agent` (or `--unlimited`, `--track record`);
- or build it yourself with `make-task`, as in the quickstart below; that gives the same file. A package records the
  scorer version of the code that built it (`scorer_version` in its `TASK.json`), so one built at another commit can
  have another task sha256, and the intake refuses bundles made from it.

`mcb start` prints the package sha256 and the task sha256, and `./check` prints the task sha256; compare them with
the submissions README.

**no_hint packages.** An untouched no_hint package reports INVALID: its target proofs are replaced by `sorry`, and
`./check` names every target whose proof is, or uses, a replaced one (for example 175 of the 197 targets of mathlib197,
whose package keeps 25 short `rfl` proofs that later declarations need). Its `TASK.md` states the baseline: the node
count of the original proofs, which the warm package reproduces. Your proofs are scored against it once no target
uses `sorry`.

## Quickstart on the toy task

Requirements:
- `python3` 3.10 or later. The tools use only the standard library.
- `git`.
- The Lean toolchain `leanprover/lean4:v4.32.0`, with `lean` and `lake` on `PATH`. The usual way is elan, Lean's
  toolchain manager: without elan, install it first with
  `curl -sSfL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh | sh -s -- -y --default-toolchain none`
  and put `~/.elan/bin` on your `PATH`. Then install the toolchain unless it is already there (`elan toolchain
  install` fails on an installed toolchain), and put `~/.elan/toolchains/leanprover--lean4---v4.32.0/bin` first on
  your `PATH`:

  ```sh
  elan toolchain list | grep -q 'leanprover/lean4:v4.32.0' || elan toolchain install leanprover/lean4:v4.32.0
  export PATH="$HOME/.elan/toolchains/leanprover--lean4---v4.32.0/bin:$PATH"
  ```

The quickstart takes about a minute. Run it from the repository root. It builds the toy package, starts it
the way a participant would (a checkout with no time budget, record track), checks it, swaps in a shorter proof,
submits, verifies the bundle, and logs the receipt. Everything it writes goes under `.work/quickstart/`, which git
ignores.

<!-- quickstart:begin -->
```sh
W=.work/quickstart
rm -rf "$W"
bin/mcb make-task tasks/toyarith/task_source.json --mode warm --out "$W/dist" --workdir "$W/make"
bin/mcb start "file://$PWD/$W/dist/toyarith-warm.tar.gz" --dir "$W/me" --unlimited --track record
P="$W/me/toyarith-warm"
"$P/check"
cp tests/fixtures/toy/ToyArithHand_Basic.lean "$P/project/ToyArith/Basic.lean"
"$P/check"
"$P/submit"
bin/mcb verify "$W/dist/toyarith-warm.tar.gz" "$P"/submissions/submission-*.tar.gz --workdir "$W/verify" --out "$W/receipts/toy.json"
bin/mcb log init "$W/log"
bin/mcb log append "$W/log" "$W/receipts/toy.json"
bin/mcb log verify "$W/log" --receipts "$W/receipts"
```
<!-- quickstart:end -->

The output should contain these lines, in this order:

<!-- quickstart-output:begin -->
```text
RUN.json: budget unlimited, track record
VALID  expr_nodes_with_levels_v1: total=20190 baseline=20190 reduction=0.0%
  compression factor 1.00x  nodes remaining 20190  nodes removed 0
VALID  expr_nodes_with_levels_v1: total=8331 baseline=20190 reduction=58.737%
  compression factor 2.42x  nodes remaining 8331  nodes removed
run: budget unlimited, elapsed
submission ID (sha256 of bundle):
ACCEPT (
LOG OK
```
<!-- quickstart-output:end -->

The `ACCEPT` line continues with the receipt status and sandbox: `(unverified, sandbox none)` on macOS or without
bubblewrap, `(verified, sandbox bwrap)` in the Linux sandbox.

What each step does:

| step | command | what it does |
|---|---|---|
| build | `mcb make-task` | Builds a task package from `tasks/<id>/task_source.json`, both a directory and a deterministic tarball. `--mode warm` includes the original proofs; `--mode no_hint` replaces them with `sorry` |
| start | `mcb start <url> [--budget-seconds N \| --unlimited] [--track agent\|record]` | Downloads the package, checks its sha256 against the published `.sha256` file, checks the manifest and the toolchain, and writes `RUN.json`: this checkout's budget (seconds or `unlimited`, the default), track, start time and scorer version. The budget belongs to the checkout, not to the task |
| check | `./check` | Compiles your sources with the pinned toolchain, checks statements, frozen definitions and axioms, and prints the proof-term node count with the compression factor (baseline / node count), nodes remaining and nodes removed. The package runs the same code as the official verifier |
| submit | `./submit [--log DIR] [--tokens-json FILE] [--track agent\|record]` | Checks again, then writes `submissions/submission-<first 16 hex digits of the ID>.tar.gz` and prints the full submission ID, the bundle's sha256. The bundle records the budget and track from `RUN.json`, the time elapsed since `start` and the token usage declared with `--tokens-json`. `--log` adds your agent trajectory, which gets hashed |
| verify | `mcb verify <package> <bundle>` | Anyone can run this. It rebuilds the bundle's source, re-checks and re-scores it, and writes a receipt with a hash chain (`receipt_root`) |
| log | `mcb log init / append / verify` | Keeps an append-only git log of receipts. `verify` catches rewritten, removed, reordered or inserted entries |

**The task specification is the instruction.** Every package has a self-contained `TASK.md` (and the same facts in
`TASK.json`): what to do, the objective (the proof-term node count) with a short definition and examples, what may
change and what must not, the allowed axioms, the verification limits, the commands and how results are reported. It
sets no time limit. An agent needs one line, for example "Read TASK.md in this directory and do the task." Results
are compared only within one budget class, and there is always an unlimited class.

**Limits.** Only one resource limit decides validity: Lean's deterministic heartbeat count, at most
`limits.kernel_heartbeats` per declaration when the kernel re-checks your files (a declaration over it is INVALID,
with its name). Time and memory never decide validity. A run that reaches a safety limit (wall-clock time, memory,
number of processes, disk written) prints `NO VERDICT` and exits 3; the organizers re-run such a submission on
another machine. On macOS, `./check` runs without a sandbox, and a process that double-forks into its own session
there is not counted against the memory, process and disk limits; the official verification runs in a Linux
sandbox, where every process is counted.

Builds happen at one canonical path, `/private/tmp/lcbench-build/<task sha256[:12]>/project` (on Linux without the
sandbox: `$LCBENCH_BUILD_ROOT`, else `lcbench-build` in `$TMPDIR` when that is not under `/tmp`, else
`~/.cache/lcbench-build`), because Lean's compiled `.olean` files record the absolute source path. The compiled
files stay on your machine: a bundle does not carry them. The
`<task sha256[:12]>` directory exists only while a check, submit or verify runs: each run deletes it at the end. To
see what was checked, look in the workspace's `.work/check/sources/` (the copy of your files that was built), and
use `./check --verbose` (what each declaration costs) or
`./check --json` (whose `log_tail` holds the Lean output of a failed build). The build root also keeps the compiled
metric tool (`tools/`) and one small lock file per task; it is safe to delete when no check or verify is running.

## The lcbench-v1 tasks: dependencies

All seven lcbench-v1 tasks import Mathlib. Their `TASK.json` pins every Lake package of the closure by git revision:
Mathlib `81a5d257c8e410db227a6665ed08f64fea08e997` and its dependencies (batteries, aesop, Qq, plausible,
proofwidgets, importGraph, LeanSearchClient, Cli). There are two ways to provide them.

1. **Let `start` fetch them.** Run `mcb start <package> --dir <workspace>` without `--deps`. The task is set up in
   `<workspace>/<task>/` (for example `<workspace>/ramsey96-warm/`); `start` copies the project's Lake files into
   `<workspace>/<task>-deps/`, next to it, runs `lake exe cache get` there (Lake clones the pinned packages into
   `<workspace>/<task>-deps/.lake/packages/` and downloads Mathlib's compiled cache) and links `project/.lake/packages`
   to that folder. `start` prints the folder; pass it with `--deps` to set up another copy without fetching again. On Linux
   with an empty home directory this took about 80 s and 7.9 GB in the dependency folder, plus 0.4 GB of download
   cache in the home directory; the following untouched `./check` gave the exact baseline. On a slow network the fetch
   can take much longer (an hour in one test). Plan for about 11 GB for a first Mathlib task: the dependency folder
   (about 7.8 GB) and about 3.2 GB in the home directory (the Lean toolchain under `~/.elan`, about 2.8 GB, and Lake's
   download cache).
2. **Reuse one dependency directory.** Build it once and pass its `packages` directory with `--deps` to
   `make-task`, `start` and `verify` (it may be read-only):

   ```sh
   mkdir -p deps && cd deps
   cp <package>/project/lakefile.toml <package>/project/lake-manifest.json <package>/project/lean-toolchain .
   lake exe cache get          # clones the pinned packages into .lake/packages and fetches Mathlib's cache
   ls .lake/packages           # aesop batteries Cli importGraph LeanSearchClient mathlib plausible proofwidgets Qq
   ```

   Then pass `--deps "$PWD/.lake/packages"`. Every package must be a git checkout at its pinned revision with the
   pinned `origin` URL; `start` and `verify` refuse anything else. Use a working `git` first on `PATH`: if `git
   remote get-url` fails, Lake treats the URL as changed and deletes the package to clone it again.

## Documentation

| file | contents |
|---|---|
| [docs/RULES.md](docs/RULES.md) | tracks, validity rules, the sharing policy, the challenge window |
| [docs/METRIC.md](docs/METRIC.md) | the normative definition of `expr_nodes_with_levels_v1` and the recorded metrics |
| [docs/SUBMITTING.md](docs/SUBMITTING.md) | submitting by pull request: tracks, `metadata.yaml`, what the organizers' check reports, the leaderboard |
| [docs/VERIFY.md](docs/VERIFY.md) | checking a receipt, its signature and the receipt log, and re-verifying a bundle yourself |
| `tasks/<id>/CARD.md` | each task: source, license, modules, targets, baselines, measured cost |

## Repository layout

| path | contents |
|---|---|
| `bin/mcb` | command-line entry point (`PYTHONPATH=src python3 -m lcbench`) |
| `src/lcbench/` | the tools (standard library only); `runtime/benchkit.py` is copied into every task package |
| `lean/` | the Lake package `lcbench_tools` (metric counter and kernel checks), also copied into every task package |
| `tasks/<id>/` | task sources: `task_source.json`, pinned Lake files, and originals when their license allows redistribution |
| `sets/` | task sets: the hashes of their packages and their baselines |
| `templates/metadata.yaml` | the metadata file of a submission |

## License

The repository is released under the MIT License ([LICENSE](LICENSE)). Each task's upstream sources keep their own
licenses, which are recorded in its `tasks/<id>/CARD.md`; originals whose license allows it are included, the others
are fetched by URL@commit + sha256.
