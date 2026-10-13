# MathCompressionBench metrics (normative)

This document defines the two metrics that MathCompressionBench computes for a submission. It is normative: the code in
`lean/LcbenchTools/Metric.lean`, `lean/Main.lean` and `src/lcbench/export_reader.py` implements exactly
this text, and the organizers' test suite checks the worked examples in section 6 against both implementations.

| metric id | role | unit |
|---|---|---|
| `expr_nodes_with_levels_v1` (the proof-term node count) | primary: ranks submissions | tree nodes |
| `dag_nodes_v1` | recorded in every receipt, never ranked in v1 | distinct nodes |

**Ids are immutable.** Changing any rule below (what is charged, how a node is counted) creates a
new metric id, for example `expr_nodes_with_levels_v2`. An existing id is never edited. Metric ids are part of
every `TASK.json` hash and every receipt.

## 1. The scored object

All metrics are computed from kernel declarations (`Lean.ConstantInfo`) of the environment obtained by
importing the organizer-rebuilt modules in a fresh process. `.olean` bytes are never scored. The counter is
a compiled executable (`lcbench-metric`); a compiled Lean program never enables initializer execution, so
`initialize` code of the imported (participant) modules does not run while the metric is computed (the test suite
checks this with a module whose initializer would leave a mark). Participant elaboration never runs in this process:
modules are only imported.

## 2. Charged declarations

**Background.** A task names its background modules in one or more of three forms: prefixes, an explicit
module list, and a list whose import closure is background (`import_closure_of`, the form used
for slt26, slt49, mathlib197 and mathlib372). A module is background iff it is listed explicitly, or equals a
listed prefix `P` or starts with `P.` (the rule of `benchkit.in_background`), or is imported, directly or
transitively, by a module of `import_closure_of` (or is one). A constant is background iff **some**
background module contains it. (Lean can store one realized constant, such as an equation lemma `f.eq_1`, in
several modules and maps its name to the first module imported; the evaluator looked names up in the
background environment, so a copy in any background module makes the constant free; the test suite has a case for
it.) Everything else reachable from the targets is charged. Core modules
(`Init`, `Std`, `Lean`) are background only if the task says so (every lcbench-v1 task does).

**Closure.** The charged set is the closure of the targets under the reference evaluator's dependency rule:

1. The queue starts with the targets, sorted by their string form.
2. Pop a name. Missing from the environment: error `CHARGED_DEPENDENCY_MISSING <name>`. Background: skip
   it (a target in the background is an error, `TARGET_IN_BACKGROUND <name>`). Already charged: skip it.
3. Otherwise it is charged. Its kind must be theorem, definition, opaque, inductive, constructor or recursor
   (else `UNSUPPORTED_CHARGED_DECLARATION <name>`; axioms and quotient constants are never charged). It must not
   be unsafe or partial, and its type and value must not contain `sorryAx` (else
   `FORBIDDEN_CHARGED_DECLARATION <name>`). Non-targets are appended to the helper list in the order they
   are charged.
4. Append its dependencies to the queue, in this order: the constants of its type, then of its value
   (`Expr.getUsedConstants` order, then the structure names of `proj` nodes in first-occurrence order); for an
   inductive, its `all` list then its constructors; for a constructor, its inductive; for a recursor, its `all`
   list, then for each rule the rule's constructor and the constants of the rule's right-hand side. For an
   inductive, finally every recursor of every member of its `all` list (kernel `recInfo` constants,
   sorted by string).

Auxiliary declarations (`_proof_N`, `match_N`, `eq_N`, `.below`, `.brecOn`, `_private` names, `.rec`) are
charged exactly when they are reached and live in a charged module; there is no name-based exception.

## 3. `expr_nodes_with_levels_v1` (the proof-term node count)

**What each charged declaration pays.** A target pays the tree size of its value (its statement is fixed by
the task and is free). Every other charged declaration pays the tree size of its type, plus its value if it has
one (definitions, theorems, opaques), plus the right-hand side of each recursor rule (recursors). Inductives
and constructors pay their type only.

**Tree size.** Expressions are counted as trees: a subterm that occurs twice is counted twice.

| constructor | nodes |
|---|---|
| `bvar i` | 1 |
| `sort u` | 1 + levels(u) |
| `const c [u1..un]` | 1 + levels(u1) + ... + levels(un) |
| `app f a` | 1 + tree(f) + tree(a) |
| `lam x t b`, `forallE x t b` | 1 + tree(t) + tree(b) (binder name and binder info are free) |
| `letE x t v b` | 1 + tree(t) + tree(v) + tree(b) |
| `lit (natVal n)`, `lit (strVal s)` | 1, whatever the size of the literal |
| `mdata m e` | tree(e): metadata is free |
| `proj S i e` | 1 + tree(e) |
| `fvar`, `mvar` | cannot occur in a kernel declaration; the counter reports an error |

| level | nodes |
|---|---|
| `zero`, `param u` | 1 |
| `succ l` | 1 + levels(l) |
| `max l1 l2`, `imax l1 l2` | 1 + levels(l1) + levels(l2) |

The node count is the sum over the charged declarations. This is the rule of the reference counter (its field
`tree_nodes_with_levels`).

## 4. `dag_nodes_v1`

The charged expressions (exactly those the node count pays for) are hash-consed: `mdata` is erased everywhere and binder
names are dropped; everything else is kept (constructor, binder info, `let` non-dependency flag, bound-variable
index, literal value, projection structure name and index, constant name and universe levels). Two nodes are
the same iff these fields and their children are the same. `dag_nodes_v1` is the number of distinct expression
nodes. Universe levels and names are not counted as nodes. Target statements are not charged and do not count.

`dag_nodes_v1` measures how much structure the charged terms contain if every repeated subterm were stored once.
It is recorded so that sharing strategies (auxiliary lemmas, `let`, metaprograms that factor terms) are
visible; the node count, the ranking metric, charges the tree (see `docs/RULES.md`, Sharing).

## 5. Implementations and interfaces

**`lcbench-metric` (Lean, compiled).** `lake build` in `lean/` produces `lean/.lake/build/bin/lcbench-metric`.
Run it with the task's `LEAN_PATH`, for example `lake env <exe> request.json` in a built task project.
Request (`lcbench.metric_request.v1`):

```json
{"modules": ["MulticolorTriangleRamsey"],
 "targets": ["ErdosProblems.MulticolourTriangleRamsey....", "..."],
 "background": {"prefixes": ["Init", "Std", "Lean", "Mathlib", "..."], "modules": [], "import_closure_of": []},
 "export": "/abs/path/export.ndjson",
 "per_declaration": true}
```

`modules` are imported (initializers never run); the three `background` fields implement section 2; `export` and
`per_declaration` are optional. The executable prints one JSON line, `lcbench.metric_result.v1`: `metrics` (the two
ids), `targets` (sorted), `helpers` (closure order), `background_refs`, `lean_githash`, `export` (`path`, `lines`),
`ms` (import, closure, the node count, and the export with `dag_nodes_v1`), and optionally `declarations`
(per-declaration node count). On failure it prints
`{"schema": ..., "error": "<NAMED_ERROR> <name>"}` and exits 1.

**Export (`lcbench.metric_export.v1`, NDJSON).** Line 1 is a header (`schema`, `lean_githash`, `modules`,
`targets`, `background`). Then records, each written after everything it refers to, with dense ids that
increase in file order:

| record | fields |
|---|---|
| name | `n` id (>= 1; 0 is the anonymous name), `p` prefix id, `s` string or `i` number |
| level | `l` id, `k` in `z s m im p`; `a`, `b` child level ids; `n` name id for `p` |
| expression | `e` id, `k` in `bv so c ap la pi le nl sl pj`; `i` (bvar index / proj index), `u` (level), `n` + `us` (const), `f` `a` (app), `t` `b` `bi` (lam/pi; bi 0-3 = default/implicit/strictImplicit/instImplicit), `t` `v` `b` `nd` (let), `v` (literal; nat as a decimal string), `s` `x` (proj) |
| declaration | `d` name id, `name`, `kind`, `role` (`target`/`helper`), `module`, `lp` level-param name ids, `type`, `value` (or null), `rules` `[[ctor, nfields, rhs]]`, plus the declaration's kernel fields (for example `numParams`, `hints`, `safety`) |

The expression table is the hash-consed DAG of section 4, followed by the target statements. The export is
deterministic: the same environment gives byte-identical files on macOS and Linux.

**Python reader (`src/lcbench/export_reader.py`, stdlib only).** `recount(path)` rejects malformed exports
(`ExportError`: out-of-order ids, dangling or forward references, unknown kinds) and recomputes the node
count and `dag_nodes_v1` from the export alone: tree sizes bottom-up over the ids, and a fresh hash-consing of the nodes
reachable from the charged expressions (so a duplicated record counts once). CLI: `mcb export-recount
EXPORT [--per-declaration]`.

**Runner (`src/lcbench/metric.py`).** `run_metric(request, project, exe=None, crosscheck=False, env=None)` runs
the executable with `lake env` in `project`, adds `environment` (`wall_s`, `peak_rss_bytes` of the counter's
process tree, `export_sha256`) and, with `crosscheck`, the Python recount; it raises
`RuntimeError` if the counter fails or the two implementations disagree. CLI: `mcb metric REQUEST
--project DIR [--crosscheck] [--exe EXE] [--out FILE]`.

## 6. Worked examples

Each case is added with an exact kernel term (`addDecl`) and computed by hand; the test suite compares the counter
and the Python recount with the hand computation. Each case is one target.

| case | value term | nodes | dag |
|---|---|---|---|
| sortZero | `Sort 0` | 2 | 1 |
| sortSuccParam | `Sort (u+1)` | 3 | 1 |
| sortMaxImax | `Sort (max u (imax v 0))` | 6 | 1 |
| const | `Nat` | 1 | 1 |
| constLevels | `PUnit.{u+1}` | 3 | 1 |
| app | `Nat.succ Nat.zero` | 3 | 3 |
| appShared | `Nat.add Nat.zero Nat.zero` | 5 | 4 |
| lamBvar | `fun x : Nat => x` | 3 | 3 |
| forallE | `Nat → Nat` | 3 | 2 |
| letE | `let x : Nat := Nat.zero; x` | 4 | 4 |
| natLit | literal `5` | 1 | 1 |
| bigNatLit | literal `2^100` | 1 | 1 |
| strLit | literal `"lcbench"` | 1 | 1 |
| mdata | `mdata (Nat.succ Nat.zero)` | 3 | 3 |
| mdataNested | `Nat.add Nat.zero (mdata Nat.zero)` | 5 | 4 |
| proj | `(Prod.mk.{0,0} Nat Nat Nat.zero Nat.zero).1` | 12 | 8 |
| useHelper | `Spec.helperValue` (a charged helper `: Nat := Nat.zero`) | 3 | 3 |
| useTwo | `Spec.Two.a` (charges `Spec.Two`, `.a`, `.b`, `.rec` with its 2 rules) | 52 | 22 |

## 7. Reference values and resources

Each task's baseline node count and `dag_nodes_v1` are in its `tasks/<id>/CARD.md` (the node count also in
[sets/lcbench-v1.json](../sets/lcbench-v1.json)); every package reproduces them exactly, and the Python recount of the
export gives the same values. A task's resource limits (the kernel heartbeat budget per declaration, and the memory
and wall-clock safety limits) are its `TASK.json` `limits`, which its `CARD.md` lists; they apply to `./check` and
`mcb verify` alike.

## 8. Known limitations of the v1 metrics

* Literals are one node in the node count whatever their size, and kernel computation (`decide`, `Nat` literal
  arithmetic) is free. A literal-size and kernel-work metric is planned as
  `expr_nodes_with_levels_v2`.
* The node count charges the tree of each charged declaration. Sharing written into the kernel terms (a `let`, or an
  auxiliary declaration referenced by name, whether written by hand or by a metaprogram) lowers the node count, because the
  shared part is counted once; sharing that exists only as pointer sharing in memory does not. `dag_nodes_v1`
  shows the size if every repeated subterm were stored once.
* The closure charges everything reachable in non-background modules; auxiliary declarations are not exempt.
