# Submitting to the MathCompressionBench leaderboard

This is the full specification for participants: the tracks, how entries are ranked, what a submission contains,
the rules, how to open the pull request, what the organizers' check posts on it, and when the leaderboard changes.

The submissions repository's README has the same steps in short.

## In short

1. You open a pull request to the submissions repository, https://github.com/MathCompressionBench/mcb-submissions, from
   your fork of it. It adds your bundles (the files `./submit` writes) and one `metadata.yaml` that describes your
   system.
2. The organizers' check rebuilds and re-scores every bundle on the organizers' machines. It posts the verdict, the
   node count and the organizers' receipt as a comment on your pull request.
3. An organizer reviews your `metadata.yaml` and merges the pull request.
4. On every merge, the leaderboard data is regenerated from the receipts of every merged submission, and the site is
   rebuilt.

## Tracks

There are three tracks. Each has its own leaderboard table.

| track | who | time limit per run | human help |
|---|---|---|---|
| `open` (the main track) | anyone, by any means | none | allowed, but it must be declared; every entry shows a "human-assisted" flag |
| `agent-60` | an agent alone | 60 minutes of wall-clock time | none |
| `agent-120` | an agent alone | 120 minutes of wall-clock time | none |

- `open` is the headline track.
- `agent-60` and `agent-120` are controlled tracks with the same limits as the study's runs (the study:
  the organizers' runs of six agents on the seven tasks of lcbench-v1 (September 2026)). Start every run of an
  agent-track submission with `mcb start <package> --track agent --budget-seconds 3600` (or `7200` for
  `agent-120`). The bundle records the track and the budget, the organizers' receipt records them again, and a run
  that is not an agent run, or whose budget is over the limit, does not count on that track.
- An agent-track result also counts on `open`, because it is agent-only. The study's runs are listed this way: each
  run on the track of its time limit (60 or 120 minutes), and all of them on `open`.
- `mcb start --track` records the kind of run in the bundle: `--track agent` for an agent working alone,
  `--track record` for a run by any means (including by hand), or nothing. A `record` run, or one without a track,
  counts on `open` only ([RULES.md](RULES.md), Tracks).
- "Human-assisted" means a person did more than start the runs: chose or edited proofs, steered the agent during a run,
  combined results by hand, and so on. If you are unsure, declare it.

## Task sets

Tasks are grouped into task sets. A task set is versioned, and once it is frozen it never changes: not its tasks, not
their packages (the task sha256 the set records), not their original node counts.

- `lcbench-v1` is the seven tasks of the study: ramsey96, slt26, slt49, permanent2, compactness5, mathlib197 and
  mathlib372 ([sets/lcbench-v1.json](../sets/lcbench-v1.json)). Submissions use their `warm` packages, for example
  `ramsey96-warm`. This release is the release candidate `lcbench-v1-rc.11`: its packages, built by scorer
  `1.0.0-rc.11`, are the assets of the GitHub release of that name, and the set's status is `candidate` until the
  final release freezes it. Each task's no_hint package is open too; an untouched no_hint package reports INVALID
  (its target proofs are `sorry`) until your proofs replace them. The packages of the earlier release candidates
  `lcbench-v1-rc.10.3` and `lcbench-v1-rc.10.2` (the same node counts) are still accepted; the organizers verify
  their bundles with the tools of those releases. These tasks use Lean v4.32.0.
- `oai-v1` is 175 tasks made from the published math challenges of https://github.com/openai/math (its `lean/` folder
  at commit `adc7f1241b42e322a6451854ab7e4b4c146bf78a`), one task per challenge of its main results
  ([sets/oai-v1.json](../sets/oai-v1.json); each task's card, for example
  [tasks/oai-matrix-fields/CARD.md](../tasks/oai-matrix-fields/CARD.md), describes it). These tasks use Lean v4.34.1.
  Submissions use their `warm` packages, for example `oai-matrix-fields-warm`; there are no no_hint packages, because
  a target's proof usually lives in lemmas of other editable files, so erasing the targets alone would not hide it.
  Its packages, built by scorer `1.0.0-rc.11`, are the assets of the GitHub release `oai-v1-rc.11`, and the set's
  status is `candidate` until the final release freezes it. Three challenges are not tasks: in each, a target
  statement differs from the challenge's only in the names of bound variables, and a task requires the exact
  statement (the set file lists them under `excluded`).
- More task sets may be added later. A new set never changes an existing one.
- Every leaderboard is per task set and per track. A result counts on a set only when its bundle was made from the
  package that set records.
- Which tasks are open for submissions is listed in the submissions repository's README. The toy task
  `toyarith-warm` belongs to no task set, so it is checked and commented on but not ranked.

## Ranking

Within one task set and one track:

1. For each task, take each system's **best verified final**: its smallest node count over all its verified bundles
   for that task. The node count is the proof-term node count (`expr_nodes_with_levels_v1`) in the organizers'
   receipt.
2. Rank the systems on each task by that node count, smallest first. Systems with the same node count share the mean
   of the places they span (two systems tied for first both get 1.5). A system without a verified final on the task
   (missing, rejected or not verified) ranks last; several such systems share the last places the same way.
3. The overall ranking is the **average rank** over the task set's tasks. Lower is better. Systems with the same
   average share a position.

The leaderboard shows node counts and ranks. It never states compression factors.

A **system** is the combination of `system`, `model`, `harness`, `reasoning_effort` and `human_assisted` in your
`metadata.yaml`, whether its results come from the study or from merged submissions. All merged submissions that agree
on these five fields are pooled: the best final per task over all of them counts, and each task shows which
submission (or study run) and which receipt supplied it. Names are compared without regard to case or spacing. A
submission that names one of the study's agents as the study records it (the model id, and the harness as recorded or
by its public name) joins that agent's row instead of adding a new one; if the study recorded a version you cannot
state, write that it was not recorded, for example `harness: "<harness name> (version not recorded)"`, and the entry joins
the study's row only when every other field matches. A different model, harness or effort is a different system.

## Attempts

- You may make any number of attempts, and submit any number of times. Only your best final per task counts.
- `attempts_per_task` must state truthfully how many attempts you made per task in all, including the ones you did
  not submit and those of your earlier submissions: it is your total so far, and the largest number a system has
  declared (or, for the study's agents, run) is shown beside its entry. The study's entries show their runs per task: 4 on `agent-60`, 2 on
  `agent-120` and 6 on `open`.

## What you submit

One pull request is one submission. It adds:

| file | what it is |
|---|---|
| `entries/<entry>/metadata.yaml` | one per submission: your system, the track and the tasks (below) |
| `submissions/<task>/<id>.tar.gz` | a bundle written by `./submit`; `<id>` is its SHA-256 hash |
| `submissions/<task>/<id>.json` | the bundle's small metadata file; its `entry` field names `<entry>` |
| `submissions/<task>/<id>.receipt.json` | optional: your self-receipt (`mcb verify` on your own machine) |

`<entry>` is a name you choose for this submission: lowercase letters, digits, `.`, `_` or `-`, at most 64
characters, for example `acme-prover-2026-10-12`. It must be new: a later submission takes a new entry name. A pull
request may add up to five bundles, for one or more of the tasks in `tasks`.

### metadata.yaml

Every field is required except `links` and `logs`. A submission missing a required field is not reviewed: the
organizers' check refuses the pull request and names the field.

| field | what to write |
|---|---|
| `system` | the name of your system, as the leaderboard shows it |
| `model` | the model and its version (a model id with its date or release); name every model if there are several |
| `harness` | the program that ran the model, with its version |
| `reasoning_effort` | the reasoning effort the model ran with (for example `low`, `medium`, `high`), or `none` |
| `track` | `open`, `agent-60` or `agent-120` |
| `human_assisted` | `true` or `false`; on `agent-60` and `agent-120` it must be `false` |
| `tasks` | the tasks this submission covers, for example `[ramsey96-warm, slt26-warm]`; every bundle's task must be one of them, and a listed task without a verified bundle ranks last |
| `attempts_per_task` | how many attempts you made per task in all: one whole number, or one number per task |
| `contact` | how the organizers reach you (an email address); it is never published |
| `links` (optional) | a list of links: an article, code or a write-up |
| `logs` (optional) | a link to your run logs, or a list of links |

The template, [templates/metadata.yaml](../templates/metadata.yaml), explains each field:

```yaml
system: "Acme Prover"
model: "example-model-2026-09-01"
harness: "example-harness 1.4.2"
reasoning_effort: high
track: agent-60
human_assisted: false
tasks: [ramsey96-warm, slt26-warm]
attempts_per_task: 3
contact: "you@example.org"
links: ["https://example.org/acme-prover"]
logs: "https://example.org/acme-prover/logs"
```

The file is read as a small part of YAML: `key: value` lines, lists as `[a, b]` or as indented `- item` lines, and
one level of indented `key: value` lines (for `attempts_per_task`). Put a value in double quotes when it holds `: `
or `#`. Write `true` or `false`, not `yes` or `no`. Anything else is refused with its line number, so the file is
never read differently from what you meant.

## Rules

- Bundles must be made from a task package the organizers accept (the submissions repository's README lists them),
  and must be exactly the files `./submit` wrote.
- The pull request may only add the files above. Anything else (another path, a changed or deleted file, a second
  `metadata.yaml`, a symbolic link, an executable file) makes the organizers' check refuse the whole pull request.
- Every bundle's `.json` names the entry (`"entry": "<entry>"`); `mcb intake add ... --entry <entry>` writes it.
- On `agent-60` and `agent-120`: `human_assisted: false`, and every bundle comes from a workspace started with
  `--track agent` and a budget of at most 3600 (or 7200) seconds. The organizers' check refuses the pull request
  otherwise.
- Declare honestly. The organizers may ask for the logs of any run, and may remove an entry whose metadata is not
  true.
- The task rules themselves (what may change in a task, how the node count is measured) are in
  [RULES.md](RULES.md).

## Step by step

1. **Start every run** from an accepted package, with the track's budget for an agent track:

   ```sh
   bin/mcb start "file://$PWD/<task>.tar.gz" --dir <workspace> --track agent --budget-seconds 3600   # agent-60
   bin/mcb start "file://$PWD/<task>.tar.gz" --dir <workspace> --unlimited                           # open
   ```

2. **Work, check and submit** in the workspace: `./check`, then `./submit` for the final you want to send.
3. **Fork the submissions repository**, https://github.com/MathCompressionBench/mcb-submissions, on GitHub: its "Fork"
   button, or `gh repo fork MathCompressionBench/mcb-submissions --clone=false`. You push to your fork; the organizers'
   repository takes only pull requests.
4. **Clone your fork** over https and add the organizers' repository as `upstream`:

   ```sh
   git clone https://github.com/<your account>/mcb-submissions.git
   cd mcb-submissions
   git remote add upstream https://github.com/MathCompressionBench/mcb-submissions.git
   ```

5. **Sync your fork with `upstream`, then make a branch**, before every submission. The organizers add newly
   accepted packages to `accepted-packages.json` on `main`. `intake add` (step 6) reads that file in your clone and
   `scripts/check_submission.py` (step 8) reads it on your fork's `main` (`origin/main`), so with a stale fork both
   refuse bundles made from the newly accepted packages.

   ```sh
   git switch main
   git fetch upstream
   git merge --ff-only upstream/main
   git push origin main
   git switch -c my-submission
   ```

   The "Sync fork" button on your fork's GitHub page, or `gh repo sync <your account>/mcb-submissions`, syncs
   the fork on GitHub; then run `git switch main && git pull` in your clone. If you made your branch before
   syncing, run `git merge upstream/main` on it.
6. **Add the bundles** to your branch, naming your entry.

   ```sh
   bin/mcb intake add <submissions checkout> <task> <bundle> --participant "<your name or team>" \
     --entry <entry> [--self-receipt self-receipt.json]
   ```

   `intake add` writes the track that the bundle records (from `mcb start --track`); `--track` is optional, and
   when given it must be the same.

7. **Write the metadata:** copy `templates/metadata.yaml` to `entries/<entry>/metadata.yaml` in the submissions
   checkout and fill it in. Check it:

   ```sh
   bin/mcb submission validate <submissions checkout>/entries/<entry>
   ```

8. **Check the whole pull request** before opening it: commit, then run `python3 scripts/check_submission.py` in the
   submissions checkout. It applies the organizers' rules, including the metadata, and needs only Python 3 and git.
9. **Push** the branch to your fork: `git push -u origin my-submission`.
10. **Open the pull request** against `main` of MathCompressionBench/mcb-submissions and fill in its template. GitHub offers
    it after the push, or run `gh pr create --repo MathCompressionBench/mcb-submissions --base main`. It may add up to five
    bundles ([What you submit](#what-you-submit)). The organizers' check posts one comment and edits that comment in
    place as the status changes (next section).

## What the organizers' check posts

The check runs on the organizers' machines on a schedule, not the moment you open the pull request. It keeps one
comment on your pull request, **lcbench check status**, posted and edited only by the organizers' GitHub account, and
edits it when the status changes. It shows:

- your metadata as the reviewer sees it (never the contact), and the listed tasks that have no bundle;
- for each bundle: received, its place in the queue, being checked, then the result (verified, mismatch or rejected)
  with the node count;
- for each finished check: the **receipt**, as a table row (result, node count, receipt root, entry in the
  organizers' append-only receipt log) and a folded block with the receipt's main fields (verdict, scores, problems,
  the run's track and budget, the verifier version, the receipt's sha256);
- on an agent track, whether each verified receipt qualifies for it;
- a line for the reviewer: whether the submission is ready for review.

A refused pull request gets the reasons instead. Push a new commit to fix it; the newest commit is checked again. For
example, after the check of one toy bundle on `open` (a file shortened by hand, so `human_assisted: true`). The bundle,
the scores and the receipt are real: the organizers' receipt of the toy file `tests/fixtures/toy/ToyArithHand_Basic.lean`
at scorer `1.0.0-rc.11`. Only the verifier commit is shown as a placeholder: it is the commit of the MathCompressionBench checkout
that ran the check. `verifier_sha256` names the verifier's code itself; [VERIFY.md](VERIFY.md) shows how to compute it
from your checkout.

````markdown
**lcbench check status** for commit `<your commit>`: checked: verified

**Submission** `entries/acme-prover-2026-10-12/metadata.yaml`: metadata complete

| field | value |
|---|---|
| system | `Acme Prover` |
| model | `example-model-2026-09-01` |
| harness | `example-harness 1.4.2` |
| reasoning effort | `high` |
| track | `open` |
| human-assisted | `yes` |
| tasks | `toyarith-warm` |
| attempts per task | `3` |
| links | `https://example.org/acme` |

- toyarith-warm/91ad2fa10ad2363b52d2f1db75b0d5bf32822fe428750b077e8f6fc1cf8cd0f5: verified (8,331 nodes; baseline 20,190) (the verified receipt matches the self-report)

**Receipts** (the organizers' receipts; each one is in their append-only receipt log):

| task | submission | result | node count | receipt root | log entry |
|---|---|---|---|---|---|
| toyarith-warm | `91ad2fa10ad2363b` | verified | 8,331 | `3c17e0c77ec8c4f9` | 1 |

<details><summary>receipt of toyarith-warm/91ad2fa10ad2363b</summary>

```json
{
 "bundle_sha256": "91ad2fa10ad2363b52d2f1db75b0d5bf32822fe428750b077e8f6fc1cf8cd0f5",
 "problems": [],
 "receipt_log_entry": 1,
 "receipt_root": "3c17e0c77ec8c4f9e182f4aa7a26abc322fd9c4cc9d0a2d9c8be0da61023aa48",
 "receipt_sha256": "a4da116555baebbf13c95d8d7e08c6bc4a4a0a1425230e1559d165cd09462da8",
 "run": {
  "budget_seconds": "unlimited",
  "elapsed_seconds": null
 },
 "scores": {
  "dag_nodes_v1": 740,
  "expr_nodes_with_levels_v1": 8331,
  "primary": "expr_nodes_with_levels_v1"
 },
 "status": "verified",
 "task_name": "toyarith-warm",
 "task_sha256": "6c2561ebc63ad9e5004aaadf6abd1a94fe5b949f230e3687df4897b8a517e55f",
 "track": null,
 "verdict": "ACCEPT",
 "verifier": {
  "commit": "<the MathCompressionBench commit that checked it>",
  "scorer_version": "1.0.0-rc.11",
  "verifier_sha256": "a891571ca685c3acd491f394349ca72c03f0ab7233005869222e20b049021a52"
 }
}
```

</details>

**For the maintainer:** the metadata is complete and every bundle is verified: ready for review. Merging it adds
these results to the leaderboard.
````

On `agent-60` or `agent-120`, the comment also says for each verified receipt whether it qualifies for that track:
this receipt records no agent run (`"track": null`) and no budget (`"budget_seconds": "unlimited"`), so it would count
on `open` only.

What the results mean (verified, mismatch, rejected, infra-failed, replaced, duplicate) is in the submissions
repository's README.

## Review and merge

An organizer reads your `metadata.yaml` and the comment. They merge when the metadata is complete and plausible and
the bundles are verified; they may merge only the verified bundles, or ask you to fix something first. You do not
need to do anything after opening the pull request unless the organizer asks. A pull request that is closed without
merging adds nothing to the leaderboard.

## When the leaderboard updates

After every merge, the organizers regenerate the leaderboard data: they read every merged `metadata.yaml`, find the
organizers' receipt of every merged bundle, keep the verified ones that were made from the task set's packages (and, on
an agent track, whose receipt records an agent run within the limit), and write one table per task set and track. Then
the site is rebuilt. The study's runs are in the same tables. Anything that is not counted is listed with the table,
with the reason.
