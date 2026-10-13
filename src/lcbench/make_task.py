# ABOUTME: Organizer tool `lcbench make-task`: task_source.json (lcbench.task_source.v1) -> self-contained task package.
# ABOUTME: Writes TASK.json, TASK.md, the Lake project (proofs erased in no_hint mode), ./check, ./submit, MANIFEST, tarball.
from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import sys
from pathlib import Path

from . import taskspec
from .paths import BENCHKIT, LEAN_DIR
from .runtime import benchkit as bk

SUPPORTED_METRICS = {"expr_nodes_with_levels_v1"}
DEFAULT_FORBIDDEN = ["sorry", "native_decide", "implemented_by", "extern", "unsafe", "skipKernelTC",
                     "debug.", "axiom ", "#exit", "admit", "partial "]
DEFAULT_AXIOMS = ["propext", "Quot.sound", "Classical.choice"]

SOURCE_SCHEMA = "lcbench.task_source.v1"
CHECK_SH = """#!/bin/sh
# ABOUTME: Compiles, checks and scores this task package's editable sources on the local machine.
# ABOUTME: Relocatable (resolves its own directory); needs python3 and the pinned Lean toolchain on PATH or via elan.
here=$(cd "$(dirname "$0")" && pwd -P)
exec python3 "$here/.benchmark/benchkit.py" check --package "$here" "$@"
"""
SUBMIT_SH = """#!/bin/sh
# ABOUTME: Re-checks the editable sources and writes a hash-manifested submission bundle; prints its sha256 ID.
# ABOUTME: Sends nothing: it prints how to submit the bundle by pull request to the submissions repository.
here=$(cd "$(dirname "$0")" && pwd -P)
exec python3 "$here/.benchmark/benchkit.py" submit --package "$here" "$@"
"""
CHANNELS = [
    "To submit, open a pull request that adds {bundle} to the submissions repository "
    "https://github.com/richardxd/lcbench-submissions; its README lists the steps (`lcbench intake add` copies the "
    "bundle under its full ID). Keep {bundle}: its ID {id} identifies it, and anyone can re-verify it with "
    "`lcbench verify`.",
]


SCOPE_OPEN = re.compile(r"(?:@\[[^\]]*\]\s*)?(?:(?:public|private|noncomputable)\s+)*(namespace|section|mutual)\b\s*([^\s-]*)")
SCOPE_END = re.compile(r"end\b")
THEOREM = re.compile(r"(?:@\[[^\]]*\]\s*)?(?:(?:private|protected|nonrec)\s+)*(?:theorem|lemma)\s+([^\s(:{\[]+)")


def declared_names(lines: list[str]):
    """(line index, declared name, full name) of every column-0 theorem/lemma. The full name prefixes the enclosing
    namespaces (`namespace`/`section`/`mutual` ... `end`, outside block comments and docstrings); `_root_.` resets it."""
    scopes: list[str] = []
    depth = 0
    for i, line in enumerate(lines):
        if depth == 0:
            m = THEOREM.match(line)
            if m:
                name = m.group(1)
                full = name[len("_root_."):] if name.startswith("_root_.") else ".".join([*filter(None, scopes), name])
                yield i, name[len("_root_."):] if name.startswith("_root_.") else name, full
            opened = SCOPE_OPEN.match(line)
            if opened:
                scopes.append(opened.group(2) if opened.group(1) == "namespace" else "")
            elif SCOPE_END.match(line) and scopes:
                scopes.pop()
        depth = max(0, depth + line.count("/-") - line.count("-/"))


OPEN_BRACKETS, CLOSE_BRACKETS = "([{⟨⦃", ")]}⟩⦄"
LET_KEYWORDS = {"let", "letI", "have", "haveI"}


def proof_start(block: str) -> int | None:
    """Index in a theorem block where its proof starts: the first `:=`, `where` or line-initial `| ` alternative
    outside brackets and norm bars, skipping `:=` that bind a `let`/`have` inside the statement, comments and string
    literals."""
    depth, bars, pending_let, i, n = 0, False, 0, 0, len(block)
    while i < n:
        ch = block[i]
        if block.startswith("--", i):
            i = block.find("\n", i)
            if i < 0:
                return None
            continue
        if block.startswith("/-", i):
            i = block.find("-/", i + 2)
            if i < 0:
                return None
            i += 2
            continue
        if ch == '"':
            i += 1
            while i < n and block[i] != '"':
                i += 2 if block[i] == "\\" else 1
            i += 1
            continue
        if ch in OPEN_BRACKETS:
            depth += 1
        elif ch in CLOSE_BRACKETS:
            depth = max(0, depth - 1)
        elif ch == "‖":
            bars = not bars
        elif depth == 0 and not bars:
            if block.startswith(":=", i):
                if pending_let:
                    pending_let -= 1
                else:
                    return i
                i += 2
                continue
            word = re.match(r"[A-Za-z_][A-Za-z_0-9'!?]*", block[i:]) if (i == 0 or not (block[i - 1].isalnum()
                                                                                          or block[i - 1] in "_.'")) else None
            if word:
                if word.group(0) in LET_KEYWORDS:
                    pending_let += 1
                elif word.group(0) == "where":
                    return i
                i += len(word.group(0))
                continue
            if block.startswith(("| ", "|\t"), i) and not block[block.rfind("\n", 0, i) + 1:i].strip() and i > 0:
                return i
        i += 1
    return None


# A proof that is only `rfl` makes its theorem an rfl-lemma that `dsimp` and `rfl` in later declarations of the same
# file may rely on (Mathlib's `Mat_.id_def`); it carries no hint beyond the statement itself.
DEFINITIONAL_PROOF = re.compile(r":=\s*(?:rfl|\(rfl\)|Iff\.rfl|HEq\.rfl|by\s+rfl)\s*$")


def erase_proofs(text: str, short_names: list[str], keep_rfl: bool = False, kept: list | None = None) -> str:
    """Replace each named theorem's proof (from where it starts, see `proof_start`, to the end of its block) with
    `sorry`. With `keep_rfl`, a proof that is only `rfl` (DEFINITIONAL_PROOF) is kept and its target appended to
    `kept`. A declaration matches a target whose name equals it or ends with `.<name>`; when several
    targets or declarations share a short name, the enclosing namespaces decide (a private target
    `_private.<module>.0.<full name>` matches its full name).

    A block ends at the next non-blank line starting in column 0. The result is validated afterwards by
    recompiling and comparing statement hashes, so a mis-cut is detected rather than trusted."""
    lines = text.splitlines(keepends=True)

    def exact(target: str, full: str) -> bool:
        return target == full or (target.startswith("_private.") and target.endswith(".0." + full))

    candidates: dict[str, list[tuple[int, bool]]] = {}
    for i, name, full in declared_names(lines):
        hits = [s for s in short_names if s == name or s.endswith("." + name)]
        if len(hits) > 1:
            hits = [s for s in hits if exact(s, full)]
            if len(hits) > 1:
                raise ValueError(f"declaration {name} matches several targets {hits}")
        for s in hits:
            candidates.setdefault(s, []).append((i, exact(s, full)))
    starts = {}
    for s, found in candidates.items():
        best = [i for i, is_exact in found if is_exact] or [i for i, _ in found]
        if len(best) > 1:
            raise ValueError(f"theorem {s} declared twice")
        starts[s] = best[0]
    missing = sorted(set(short_names) - set(starts))
    if missing:
        raise ValueError(f"cannot find theorems {missing}")
    out, i = [], 0
    ranges = {}
    target_at = {line: name for name, line in starts.items()}
    for name, s in starts.items():
        e = s + 1
        while e < len(lines) and (not lines[e].strip() or lines[e][0].isspace()):
            e += 1
        ranges[s] = e
    while i < len(lines):
        if i in ranges:
            block = "".join(lines[i:ranges[i]])
            cut = proof_start(block)
            if cut is None:
                raise ValueError(f"no proof start (:=, where or |) in block starting at line {i + 1}")
            head = block[:cut]
            if keep_rfl and DEFINITIONAL_PROOF.match(block[cut:]):
                out.append(block)
                if kept is not None:
                    kept.append(target_at[i])
                i = ranges[i]
                continue
            out.append(head.rstrip() + " := by\n  sorry\n\n")
            i = ranges[i]
        else:
            out.append(lines[i])
            i += 1
    return "".join(out)


def declares_any(text: str, names: list[str]) -> bool:
    """Whether the file declares a theorem that erase_proofs would match to one of `names` (its short name equals a
    name or ends it); package_audit uses it to tell a shipped original with a target from a support module."""
    return any(s == name or s.endswith("." + name)
               for _, name, _ in declared_names(text.splitlines(keepends=True)) for s in names)


def erase_task_proofs(originals: dict[str, str], targets: list[dict], module_of: dict[str, str] | None = None,
                      keep_rfl: bool = False, kept: list | None = None) -> dict[str, str]:
    """no_hint sources: in each editable file, erase the proofs of the targets declared in its module (`module_of`:
    path -> module, by default the path's module name); files without targets are unchanged."""
    module_of = module_of or {rel: taskspec.module_of_path(rel) for rel in originals}
    return {rel: erase_proofs(text, [t["name"] for t in targets if t["module"] == module_of[rel]],
                              keep_rfl=keep_rfl, kept=kept)
            for rel, text in originals.items()}


def no_hint_sources(src: dict, originals: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """(no_hint sources, targets whose `rfl` proofs were kept) for a task source: each editable file erases the targets
    of its own module (the editable entry's `module`, else its path); with `no_hint.keep_rfl_proofs` (opt-in per task)
    bare `rfl` proofs that later declarations need as rfl-lemmas stay (see DEFINITIONAL_PROOF)."""
    module_of = {e["path"]: e.get("module") or taskspec.module_of_path(e["path"]) for e in src["editable"]}
    keep_rfl = bool(src.get("no_hint", {}).get("keep_rfl_proofs"))
    kept: list[str] = []
    sources = erase_task_proofs(originals, src["targets"], module_of, keep_rfl=keep_rfl, kept=kept)
    return sources, kept


def platform_free(version: str) -> str:
    """Drop host triples such as `x86_64-unknown-linux-gnu` / `arm64-apple-darwin24.6.0` from a version string."""
    return re.sub(r",\s*[A-Za-z0-9_]+-[A-Za-z0-9_]+-[A-Za-z0-9_.\-]+\s*,", ",", version)


def extract_or_die(task: dict, project: Path, facts_only: bool = False, background_manifest: Path | None = None) -> dict:
    """Build and extract; with `facts_only` (erased no_hint sources) the counter may refuse the `sorry` targets."""
    built = bk.build_and_extract(task, project, LEAN_DIR, background_manifest=background_manifest)
    if built["stage"] != "ok" and not (facts_only and built["stage"] == "metric"):
        sys.exit(f"{built['stage']} failed:\n{built['log']}")
    return built["data"]


def background_manifest(draft: dict, project: Path, closure: list[str]) -> bytes:
    """The pinned background: every background module of the original's import closure plus every module the
    toolchain ships (a submission may import more of the toolchain, as metaprograms do), each with the digest of its
    .olean files in the organizer build. Both sets, and so the manifest, are the same on every platform."""
    pinned = {"toolchain": {"id": "(from PATH)", "lean_githash": bk.run(
        ["lean", "--githash"], project, bk.lean_env(), timeout=60).stdout.strip()}}
    toolchain, problems = bk.resolve_toolchain(pinned, project)
    if problems:
        sys.exit("; ".join(problems))
    runner = bk.DirectRunner(toolchain, project)
    digests = bk.background_module_digests(draft, bk.lean_path_dirs(runner), project,
                                           set(closure) | set(bk.toolchain_modules(toolchain)))
    missing = sorted(m for m, d in digests.items() if d == "missing")
    if missing:
        sys.exit(f"background modules without an .olean in the organizer build: {missing[:5]}")
    return bk.background_manifest_bytes(digests)


def load_source(path: Path) -> tuple[dict, dict[str, str]]:
    """Read task_source.json and every content file it names, checking each declared sha256.

    Returns (source, files) with files["fixed"] (lakefile, manifest, lean-toolchain), files["originals"]
    (editable path -> text) and files["fixed_sources"] (read-only fixed_files path -> text)."""
    src = json.loads(path.read_text())
    if src.get("schema") != SOURCE_SCHEMA:
        sys.exit(f"{path}: schema must be {SOURCE_SCHEMA}")
    base = path.parent
    fixed = {}
    proj = src["project"]
    for key, name in (("lakefile", None), ("lake_manifest", "lake-manifest.json")):
        spec = proj.get(key)
        if not spec:
            continue
        data = (base / spec["content_file"]).read_bytes()
        if bk.sha256_bytes(data) != spec["sha256"]:
            sys.exit(f"{spec['content_file']}: sha256 differs from task_source")
        fixed[name or spec["name"]] = data.decode()
    fixed["lean-toolchain"] = src["toolchain"]["id"] + "\n"
    originals = {}
    for e in src["editable"]:
        if "content_file" in e:
            data = (base / e["content_file"]).read_bytes()
            if bk.sha256_bytes(data) != e["original_sha256"]:
                sys.exit(f"{e['content_file']}: sha256 differs from task_source original_sha256")
        else:
            data = fetch_original(base, e)
        originals[e["path"]] = data.decode()
    fixed_sources = {}
    for f in src.get("fixed_files", []):
        if "content_file" in f:
            data = (base / f["content_file"]).read_bytes()
            if bk.sha256_bytes(data) != f["sha256"]:
                sys.exit(f"{f['content_file']}: sha256 differs from task_source fixed_files sha256")
        else:
            data = fetch_original(base, {"path": f["path"], "original_sha256": f["sha256"], "fetch": f["fetch"]})
        fixed_sources[f["path"]] = data.decode()
    return src, {"fixed": fixed, "originals": originals, "fixed_sources": fixed_sources}


# ---------------------------------------------------------------- leaf property

CORE_PREFIXES = ("Init", "Std", "Lean", "Lake")
HEADER_MODIFIERS = {"public", "meta", "private"}
IMPORT_MODIFIERS = {"all", "runtime"}


def _header_tokens(text: str):
    """Tokens of a Lean file with `--` and nested `/- -/` comments removed, lazily from the start."""
    i, n = 0, len(text)
    while i < n:
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
        elif text.startswith("/-", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/-", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("-/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif text[i].isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and not text.startswith("--", j) and not text.startswith("/-", j):
                j += 1
            yield text[i:j]
            i = j


def lean_imports(text: str) -> list[str]:
    """Modules imported by a Lean 4 file header (`module`, `prelude`, `[public] [meta] import [all] M`)."""
    out, expect_module = [], False
    for tok in _header_tokens(text):
        if expect_module:
            if tok in IMPORT_MODIFIERS:
                continue
            out.append(tok)
            expect_module = False
        elif tok == "import":
            expect_module = True
        elif tok in HEADER_MODIFIERS or (tok in ("module", "prelude") and not out):
            continue
        else:
            break
    return out


def text_resolver(texts: dict[str, str]):
    """Resolve a module to its source text among {relative path: text} (the task's own files)."""
    def resolve(module: str) -> str | None:
        return texts.get(bk.module_path(module) + ".lean")
    return resolve


def dir_resolver(root: Path):
    """Resolve a module to <root>/<module path>.lean (a Lake package directory)."""
    def resolve(module: str) -> str | None:
        p = root / (bk.module_path(module) + ".lean")
        return p.read_text(errors="replace") if p.is_file() else None
    return resolve


def import_graph(start: list[str], resolvers: list) -> dict[str, list[str]]:
    """Direct imports of every module reachable from `start`; core toolchain modules are not descended
    (they cannot import task modules). A non-core module whose source cannot be found is an error."""
    graph: dict[str, list[str]] = {}
    queue = list(start)
    while queue:
        m = queue.pop()
        if m in graph:
            continue
        if m.split(".")[0] in CORE_PREFIXES:
            continue
        text = next((t for t in (r(m) for r in resolvers) if t is not None), None)
        if text is None:
            sys.exit(f"leaf check: cannot find the source of module {m}")
        graph[m] = lean_imports(text)
        queue.extend(graph[m])
    return graph


def leaf_violations(editable: list[str], graph: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(importer, editable) pairs where a non-editable module of the scored import closure imports an
    editable module."""
    owners = set(editable)
    return sorted((m, imp) for m, imps in graph.items() if m not in owners for imp in imps if imp in owners)


def leaf_check(src: dict, files: dict, deps: Path | None) -> None:
    texts = {**files["originals"], **files["fixed_sources"]}
    resolvers = [text_resolver(texts)]
    if deps is not None:
        resolvers += [dir_resolver(p) for p in sorted(deps.iterdir()) if p.is_dir()]
    charged = list(src["owner_modules"]) + [f["module"] for f in src.get("fixed_files", [])
                                            if f.get("class") == "fixed_charged"]
    bad = leaf_violations(src["owner_modules"], import_graph(charged, resolvers))
    if bad:
        sys.exit("leaf property violated: " + "; ".join(f"{m} imports editable {e}" for m, e in bad))


def fetch_original(base: Path, entry: dict) -> bytes:
    """An editable original that is not vendored (license unverified): download `fetch.url` (pinned to a commit)
    once into <task dir>/fetched/<path>, keeping it only if its sha256 equals `original_sha256`."""
    dest = base / "fetched" / entry["path"]
    want = entry["original_sha256"]
    if dest.is_file() and bk.sha256_file(dest) == want:
        return dest.read_bytes()
    url = entry["fetch"]["url"]
    try:
        data = bk.fetch(url)
    except (OSError, ValueError) as e:
        sys.exit(f"cannot fetch {url}: {e}; place the file with sha256 {want} at {dest}")
    got = bk.sha256_bytes(data)
    if got != want:
        sys.exit(f"{url}: sha256 {got} differs from task_source original_sha256 {want}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    partial.write_bytes(data)
    partial.replace(dest)
    return data


def fetch_main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcbench fetch-originals")
    ap.add_argument("task_source", type=Path, help="tasks/<id>/task_source.json")
    args = ap.parse_args(argv)
    src, files = load_source(args.task_source.resolve())
    for rel, text in sorted(files["originals"].items()):
        print(f"{bk.sha256_bytes(text.encode())}  {rel}")
    return 0


OBJECTIVE_NAME = "proof-term node count"
OBJECTIVE_DEFINITION = (
    "The proof-term node count is the number of nodes in the Lean kernel proof terms that the targets need inside "
    "the scored files, universe levels included. Terms are counted as trees: every application, lambda, forall, "
    "let, constant, bound variable, sort, literal and projection is one node, every universe level inside a "
    "constant or a sort counts too, metadata is free, and a subterm that occurs twice is counted twice. Each target "
    "pays for its proof (its statement is fixed and free). Every other declaration in a scored file that a target "
    "uses, directly or through other declarations, pays for its statement and its proof or body. Declarations "
    "outside the scored files are free. Source text, comments, names and tactic scripts are not counted, only the "
    "terms they produce.")
OBJECTIVE_EXAMPLES = [("Nat.succ Nat.zero", 3, "one application and two constants"),
                      ("PUnit.{u+1}", 3, "one constant and its universe level `u+1`, which is two level nodes"),
                      ("Nat.add Nat.zero Nat.zero", 5, "two applications and three constants (the repeated "
                                                       "`Nat.zero` is counted twice)")]


def objective(metric_id: str, baseline: int, scored_modules: list[str]) -> dict:
    """TASK.json `objective`: the one quantity participants minimize, and how results are reported."""
    return {"name": OBJECTIVE_NAME, "metric": metric_id, "direction": "minimize", "baseline": baseline,
            "definition": OBJECTIVE_DEFINITION, "scored_modules": scored_modules,
            "examples": [{"term": t, "nodes": n} for t, n, _ in OBJECTIVE_EXAMPLES],
            "reported": {"nodes_remaining": "node count", "nodes_removed": "baseline - node count",
                         "compression_factor": "baseline / node count"},
            "note": "the only objective; values under metric.recorded are recorded for analysis and are not "
                    "objectives"}


def task_rules(src: dict) -> dict:
    """TASK.json `rules`: the axioms, the forbidden-text lint and, in words, how the verifier checks a submission."""
    return {"allowed_axioms": src["rules"]["allowed_axioms"],
            "forbidden_text": src["rules"].get("forbidden_text", DEFAULT_FORBIDDEN),
            "kernel_replay": "every owner module replayed by the Lean kernel (lcbench-check replay), "
                             "at most limits.kernel_heartbeats heartbeats per declaration",
            "statement_identity": "structural kernel-type hash incl. binder names and universe params",
            "frozen_identity": "kernel closure hash of each frozen definition: types, data values and propositions "
                               "of the declarations it reaches; every proof is one placeholder, and constants used "
                               "only inside proofs are not part of the closure",
            "imports": "editable modules may import each other, the other owner modules and pinned background "
                       "modules (background.sha256)"}


def export_block(kernel_export: bool) -> dict:
    """TASK.json `export`: the scored object is the kernel export rebuilt from the submitted source (with
    `kernel_export` false, the per-declaration score breakdown stands in for it in the bundle)."""
    return {"object": "kernel_export", "kernel_export": kernel_export,
            "verification": "verifier recompiles the source in a sandbox and scores the rebuilt kernel export, which "
                            "must equal the submitted one"}


def listed(names: list[str], limit: int = 12) -> str:
    return ", ".join(names) if len(names) <= limit else f"{len(names)} modules (listed in TASK.json `background`)"


def task_markdown(task: dict, src: dict) -> str:
    """TASK.md: the complete instruction for a participant. It names one objective (the proof-term node count,
    with its metric id once), the kernel heartbeat limit as the only validity limit, and no time limit; the budget
    of a checkout is in its RUN.json."""
    root, editable = task["project"]["root"], task["project"]["editable"]
    baseline = task["metric"]["baseline_total"]
    targets = [t["name"] for t in task["targets"]]
    heartbeats = task.get("limits", {}).get("kernel_heartbeats")
    files = ", ".join(f"`{root}/{e}`" for e in editable)
    fixed_scored = [f"`{root}/{bk.module_path(m)}.lean`" for m in task.get("modules", {}).get("fixed_charged", [])]
    scored = ("The scored files are the editable files" + (f" and these fixed files, which you may not edit: "
                                                           f"{', '.join(fixed_scored)}" if fixed_scored else "")
              + ".")
    lines = [f"# {src.get('title', task['name'])}", "",
             "## The task", "",
             f"`{root}/` is a Lean 4 project (toolchain `{task['toolchain']['id']}`) with {len(targets)} target "
             f"theorems. Make the proofs of the targets as small as possible, measured by their proof-term node count "
             f"(below). Smaller is better. The target statements must stay exactly as they are.", ""]
    if task["mode"] == "no_hint":
        lines += ["The proofs of the targets have been replaced by `sorry`, and the original proofs are not "
                  "included: you must write the proofs.", ""]
    else:
        lines += ["The original proofs are included; you may start from them.", ""]
    lines += ["## The objective: proof-term node count", "",
              f"{OBJECTIVE_DEFINITION} {scored} The technical name of this count is `{task['metric']['id']}`.", "",
              "Examples:"]
    lines += [f"- `{term}` has {n} nodes: {why}." for term, n, why in OBJECTIVE_EXAMPLES]
    lines += ["", f"The unmodified project has a node count of {baseline:,} (the baseline).", "",
              "Results are reported as:",
              f"- compression factor: {baseline:,} / node count (for example 2.00x means half the nodes are left);",
              "- nodes remaining: the node count of your version;",
              f"- nodes removed: {baseline:,} - node count.", "",
              "## What you may change", "",
              f"The editable files: {files}. You may rewrite them freely: proofs, helper lemmas and definitions, "
              "tactics, macros, elaborators and metaprograms that build proof terms, and you may delete "
              "declarations that are neither targets nor frozen definitions. Proofs anywhere may change, "
              "including proofs inside the frozen definitions.",
              "All other files of the project are fixed, and only the editable files are submitted.", "",
              "## What must not change", "",
              "- Every target must still exist under the same name, with the same statement (binder names, "
              "implicit arguments and universe parameters included).",
              f"- Frozen definitions ({len(task['frozen'])}, listed in TASK.json `frozen`): the definitions, "
              "structures and instances that the target statements mention, directly or indirectly. They must "
              "keep the same meaning (their types, data and propositions; the proofs inside them are not "
              "compared), so a target cannot be changed by redefining what it talks about.",
              f"- Imports: the editable files may import each other"
              + (f", the fixed scored files" if fixed_scored else "")
              + f" and modules that are already available and fixed: {listed(task['background']['modules'])}.",
              f"- Axioms: the targets may depend only on {', '.join(task['rules']['allowed_axioms'])}. `sorry`, "
              "`native_decide` and other axioms are rejected.", "",
              "## Verification", "",
              "`./check` compiles your files with the pinned toolchain, replays every editable file in the Lean "
              "kernel, checks the rules above and computes the node count. The official verification runs the same code.",
              "The kernel heartbeat limit is the only limit that decides validity. Heartbeats are Lean's "
              "deterministic count of the kernel's work, the same on every machine"
              + (f": the kernel may use at most {heartbeats:,} heartbeats per declaration when it re-checks your "
                 "files (TASK.json `limits.kernel_heartbeats`), and a declaration over it makes the submission "
                 "invalid, with its name in the reason." if heartbeats
                 else "; TASK.json `limits.kernel_heartbeats` states the limit when the task sets one."),
              "Elaboration is limited by Lean's own `maxHeartbeats` option, as the source files set it.",
              "Time and memory never decide validity. Verification stops a run that reaches a safety limit "
              "(wall-clock time, memory, number of processes, disk written) with no verdict, and the run is "
              "repeated on another machine; `./check` then prints NO VERDICT.", "",
              "## Commands", "",
              "- `./check` prints the official score: the node count and the baseline, then the compression factor, nodes "
              "remaining and nodes removed. "
              "`./check --verbose` also lists what each declaration pays.",
              "- `./submit` checks again and writes `submissions/submission-<first 16 hex digits of the id>.tar.gz` "
              "and prints the full id, the sha256 of the bundle. Options: `--log DIR` adds your log, "
              "`--tokens-json FILE` declares your token usage, `--track agent|record` declares the entry track. "
              "Nothing is sent anywhere.", "",
              "## Budget", "",
              "This task sets no time limit. `RUN.json` in this directory, written when this copy was set up, states "
              "the budget of this copy (`budget_seconds`: a number of seconds, or `unlimited`) and its entry track. "
              "The budget is enforced by whoever runs the work, not by the task. `./submit` records the budget, the "
              "time elapsed since the start and the declared token usage, and results are compared only with "
              "results under the same budget. Setting up a new copy of the task does not start a new budget: if "
              "you set up this task again, the time spent in earlier copies counts, so report the total time from "
              "your first start with your result.", "",
              "## Files", "",
              f"- `{root}/`: the Lean project.",
              "- `TASK.json`: this specification in machine-readable form (targets and their statement hashes, "
              "frozen definitions, limits, objective).",
              "- `RUN.json`: the budget and track of this copy.",
              "- `check`, `submit`, `.benchmark/`: the checking tools.", "",
              f"## Targets ({len(targets)})", ""]
    lines += [f"- `{name}`" for name in targets]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcbench make-task")
    ap.add_argument("task_source", type=Path, help="tasks/<id>/task_source.json")
    ap.add_argument("--mode", choices=["no_hint", "warm"], required=True)
    ap.add_argument("--out", required=True, type=Path, help="output directory for the package and its tarball")
    ap.add_argument("--workdir", required=True, type=Path)
    ap.add_argument("--deps", type=Path, help="directory holding the pinned Lake packages (tasks with packages)")
    ap.add_argument("--name", help="package name (default <task_id>-<mode>)")
    args = ap.parse_args(argv)
    src, files = load_source(args.task_source.resolve())
    errors = taskspec.lint(src, args.task_source.resolve().parent)
    if errors:
        sys.exit("task source fails lint:\n  " + "\n  ".join(map(str, errors)))
    deps = args.deps.resolve() if args.deps else None
    leaf_check(src, files, deps)
    classes = taskspec.ModuleClasses(src, args.task_source.resolve().parent)
    if src["metrics"]["primary"] not in SUPPORTED_METRICS:
        sys.exit(f"unsupported metric {src['metrics']['primary']}")
    if args.mode not in src.get("modes", ["warm", "no_hint"]):
        sys.exit(f"mode {args.mode} not offered by this task")
    name = args.name or f"{src['task_id']}-{args.mode}"
    # charged modules = editable + fixed_charged; fixed sources are hash-pinned project files like the lakefile
    owners, originals = classes.charged, files["originals"]
    fixed = {**files["fixed"], **files["fixed_sources"]}
    editable = [e["path"] for e in src["editable"]]
    targets = [t["name"] for t in src["targets"]]
    frozen_spec = src.get("frozen", [])
    work = args.workdir.resolve()
    if work.exists():
        shutil.rmtree(work)
    manifest = json.loads(fixed["lake-manifest.json"]) if "lake-manifest.json" in fixed else {"packages": []}
    packages = [{"name": p["name"], "url": p["url"], "rev": p["rev"]} for p in manifest["packages"]]

    project_base = {"root": "project", "owner_modules": owners, "editable": editable, "packages": packages}

    def write_project(dest: Path, sources: dict[str, str]) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        for rel, text in {**fixed, **sources}.items():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            (dest / rel).write_text(text)

    def stage(dest: Path, sources: dict[str, str]) -> None:
        write_project(dest, sources)
        problems = bk.package_problems({"project": project_base}, deps)
        if problems:
            sys.exit("dependency closure: " + "; ".join(problems))
        if packages:
            (dest / ".lake").mkdir(exist_ok=True)
            (dest / ".lake/packages").symlink_to(deps)

    stage(work / "original", originals)
    version = bk.run(["lean", "--version"], work / "original", bk.lean_env()).stdout.strip()
    githash = bk.run(["lean", "--githash"], work / "original", bk.lean_env()).stdout.strip()
    lake_version = bk.run(["lake", "--version"], work / "original", bk.lean_env()).stdout.strip()
    if githash != src["toolchain"]["githash"]:
        sys.exit(f"lean on PATH has githash {githash!r}, task_source pins {src['toolchain']['githash']}")
    kernel_export = src.get("export", {}).get("kernel_export", True)
    explicit_frozen = [f["name"] for f in frozen_spec] if isinstance(frozen_spec, list) else []
    background = {"modules": src["background"]["modules"],
                  "rule": "a module is background iff it equals or is prefixed by a listed module",
                  **({"explicit_modules": sorted(classes.explicit)} if classes.explicit else {}),
                  **({"fixed_modules": classes.fixed_background} if classes.fixed_background else {})}
    draft = {"project": project_base, "targets": [{"name": t} for t in targets], "background": background,
             "frozen": [{"name": f} for f in explicit_frozen], "export": {"kernel_export": kernel_export}}
    data = extract_or_die(draft, work / "original")
    derived = data["statement_closure"]
    if frozen_spec == "statement_closure":
        draft["frozen"] = [{"name": n} for n in derived]
        data = extract_or_die(draft, work / "original")
    else:
        missing = [n for n in derived if n not in explicit_frozen]
        if missing:
            sys.exit(f"frozen set incomplete: {', '.join(missing)} (reachable from a target statement; the frozen "
                     f"list may only add to the mechanically derived set)")
    manifest = background_manifest(draft, work / "original", data["owner_import_closure"])
    (work / "background.sha256").write_bytes(manifest)
    background.update(olean_manifest=bk.BACKGROUND_MANIFEST, olean_sha256_root=bk.sha256_bytes(manifest),
                      olean_modules=len(bk.read_background_manifest(manifest)),
                      olean_rule="every background module of the original's import closure and every toolchain "
                                 "module, pinned by the sha256 of its .olean files; re-checked after the build")

    statements = {r["name"]: bk.statement_sha256(r["statement_ser"]) for r in data["targets"]}
    for t in src["targets"]:
        if t.get("statement_sha256") and statements.get(t["name"]) != t["statement_sha256"]:
            sys.exit(f"target {t['name']}: statement hash {statements.get(t['name'])} != task_source")
    closures = {r["name"]: bk.frozen_closure_sha256(r) for r in data["frozen"] if not r["missing"]}
    if isinstance(frozen_spec, list):
        for f in frozen_spec:
            if f.get("closure_sha256") and closures.get(f["name"]) != f["closure_sha256"]:
                sys.exit(f"frozen {f['name']}: closure hash differs from task_source")
    computed = {src["metrics"]["primary"]: data["total"], **{m: data[m] for m in bk.RECORDED_METRICS}}
    for metric_id, got in computed.items():
        want = src.get("baseline", {}).get(metric_id)
        if want is not None and got != want:
            sys.exit(f"baseline mismatch: {metric_id} computed {got}, task_source says {want}")

    recorded = {m: {"baseline": computed.get(m, src.get("baseline", {}).get(m)),
                    "status": "computed by lcbench-metric" if m in computed
                    else "not computed by lcbench (recorded from the audited baseline only)"}
                for m in src["metrics"].get("recorded", [])}
    task = {
        "schema": bk.TASK_SCHEMA,
        "name": name,
        "task_id": src["task_id"],
        "mode": args.mode,
        # only platform-independent toolchain facts: `lean --version` embeds the host triple, which would make
        # TASK.json (and so the task hash) differ between organizer machines
        "toolchain": {"id": src["toolchain"]["id"], "lean_githash": githash,
                      "lean_version": platform_free(version), "lake_version": platform_free(lake_version)},
        "source": {"repository": src.get("repository", "local-tree"), "commit": src.get("commit"),
                   "original_sources_sha256": {rel: bk.sha256_bytes(t.encode()) for rel, t in originals.items()},
                   "originals_published": args.mode == "warm",
                   "license": src["source_license"]["spdx"],
                   "redistribution": src["source_license"]["redistribution"],
                   "commit_date": src["source_commit_date"]},
        "project": {**project_base, "fixed_files": {rel: bk.sha256_bytes(t.encode()) for rel, t in fixed.items()}},
        "background": background,
        "task_revision": src["task_revision"],
        "scorer_version": bk.SCORER_VERSION,
        "modules": {"editable": classes.editable, "fixed_charged": classes.fixed_charged,
                    "charged": classes.charged, "core": {"modules": src["core"]["modules"],
                                                         "class": src["core"]["class"]},
                    "frozen_module": src.get("frozen_module")},
        "leaf_check": "pass",
        "frozen_derivation": {
            "rule": "every non-theorem charged declaration reachable from a target statement (types and values of "
                    "definitions, statements of theorems, inductive families, constructors and recursors); a "
                    "hand list may only add to it",
            "derived": len(derived),
            "hand_added": [n for n in explicit_frozen if n not in derived],
            "proofs": "irrelevant: a frozen definition is its meaning (types, data values and propositions of the "
                      "declarations in its closure); every proof inside them (a subterm whose type is a proposition) "
                      "is one placeholder, and constants used only inside proofs (auxiliary _proof_N theorems, "
                      "lemmas) are not part of the closure, so no proof is pinned or compared"},
        "limits": src["limits"],
        "tools": src["tools"],
        "targets": [{"name": r["name"], "statement_sha256": statements[r["name"]]} for r in data["targets"]],
        "frozen": [{"name": r["name"], "closure_sha256": closures[r["name"]]} for r in data["frozen"]],
        "rules": task_rules(src),
        "export": export_block(kernel_export),
        "objective": objective(src["metrics"]["primary"], data["total"], classes.charged),
        "metric": {"id": src["metrics"]["primary"], "baseline_total": data["total"], "baselines": computed,
                   "recorded": recorded,
                   "charging": "targets: value nodes; every other owner-module declaration reached: "
                               "type + value + recursor-rule nodes; background declarations: free",
                   "score": "reduction_pct = 100 * (baseline_total - total) / baseline_total"},
        "commands": {"start": "lcbench start <package url> --dir DIR [--deps DIR] [--budget-seconds N | --unlimited] "
                              "[--track agent|record]", "check": "./check",
                     "submit": "./submit [--log DIR] [--tokens-json FILE] [--track agent|record]",
                     "verify": "lcbench verify <package> <bundle>"},
        "submission_channels": CHANNELS,
    }
    problems = bk.check_extraction(task, data, originals)
    if problems:
        sys.exit("original sources do not pass the task rules:\n  " + "\n  ".join(problems))

    if args.mode == "no_hint":
        # each editable file erases the targets declared in its own module
        sources, kept = no_hint_sources(src, originals)
        keep_rfl = bool(src.get("no_hint", {}).get("keep_rfl_proofs"))
        stage(work / "erased", sources)
        erased = extract_or_die(task, work / "erased", facts_only=True,
                                background_manifest=work / "background.sha256")
        problems = [p for p in bk.check_extraction(task, erased, sources) if "sorry" not in p]
        missing_sorry = [t["name"] for t in erased["targets"] if "sorryAx" not in t["axioms"] and t["name"] not in kept]
        if problems or missing_sorry:
            sys.exit(f"proof erasure changed the task: {problems} / targets not erased: {missing_sorry}")
        if keep_rfl:
            task["no_hint"] = {"erased_targets": len(erased["targets"]) - len(kept),
                               "rfl_proofs_kept": sorted(kept), "every_erased_target_uses_sorryAx": True,
                               "statement_hashes_equal_warm": True}
        else:
            task["no_hint"] = {"erased_targets": len(erased["targets"]), "every_target_uses_sorryAx": True,
                               "statement_hashes_equal_warm": True}
    else:
        sources = originals

    pkg = args.out.resolve() / name
    if pkg.exists():
        shutil.rmtree(pkg)
    write_project(pkg / "project", sources)
    (pkg / ".benchmark").mkdir(parents=True)
    (pkg / bk.BACKGROUND_MANIFEST).write_bytes(manifest)
    shutil.copyfile(BENCHKIT, pkg / ".benchmark/benchkit.py")
    for rel in bk.METRIC_TOOL_FILES:
        (pkg / ".benchmark/tools" / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LEAN_DIR / rel, pkg / ".benchmark/tools" / rel)
    for script, body in (("check", CHECK_SH), ("submit", SUBMIT_SH)):
        (pkg / script).write_text(body)
        (pkg / script).chmod((pkg / script).stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    task["tooling_sha256"] = {rel: bk.sha256_file(pkg / rel)
                              for rel in (".benchmark/benchkit.py", bk.BACKGROUND_MANIFEST,
                                          "check", "submit",
                                          *(f".benchmark/tools/{t}" for t in bk.METRIC_TOOL_FILES))}
    (pkg / "TASK.json").write_text(bk.pretty_json(task))
    (pkg / "TASK.md").write_text(task_markdown(task, src))
    bk.write_manifest(pkg)
    tarball = args.out.resolve() / f"{name}.tar.gz"
    tar_sha = bk.deterministic_tar(pkg, tarball, name)
    (args.out.resolve() / f"{name}.tar.gz.sha256").write_text(f"{tar_sha}  {tarball.name}\n")
    print(json.dumps({"package": str(pkg), "tarball": str(tarball), "tarball_sha256": tar_sha,
                      "task_sha256": bk.sha256_file(pkg / "TASK.json"), "baseline_total": data["total"],
                      "targets": len(task["targets"]), "frozen": len(task["frozen"])}, indent=1))
    return 0


COMMANDS = {"make-task": ("build a task package (warm or no_hint) from tasks/<id>/task_source.json", main),
            "fetch-originals": ("download and sha256-check a task's non-vendored originals", fetch_main)}


if __name__ == "__main__":
    sys.exit(main())
