# ABOUTME: lcbench.task_source.v1 (tasks/<id>/task_source.json): module classes and the linter behind `lcbench tasks lint`.
# ABOUTME: Offline and stdlib only: checks required fields, pins, licenses, content hashes and cluster-path hygiene.
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

SCHEMA = "lcbench.task_source.v1"
KNOWN_METRICS = {"expr_nodes_with_levels_v1", "dag_nodes_v1"}
PRIMARY_METRICS = {"expr_nodes_with_levels_v1"}
STATUSES = {"prototype", "draft", "candidate", "frozen", "retired"}
MODES = {"warm", "no_hint"}
FIXED_CLASSES = {"fixed_charged", "background"}
LIMIT_KEYS = ("kernel_heartbeats", "mem_mb")
# optional limits; the wall-clock safety cap only ever gives an infrastructure timeout, never an invalid verdict
OPTIONAL_LIMIT_KEYS = ("safety_wall_s", "max_processes", "disk_mb")
# wall-clock limits would make validity depend on the machine's speed (a slow node rejects what a fast one accepts)
WALL_CLOCK_LIMIT_KEYS = ("build_s", "kernel_replay_s", "export_s")
REDISTRIBUTION = {"vendored", "fetch"}
CORE_MODULES = ["Init", "Std", "Lean"]
REQUIRED = ("schema", "task_id", "title", "task_revision", "status", "revision_history", "toolchain", "project",
            "background", "core", "editable", "owner_modules", "targets", "frozen", "rules", "metrics", "baseline",
            "modes", "tools", "limits", "source_license", "source_commit_date", "provenance")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
COMMIT_IN_URL_RE = re.compile(r"(?:^|[/@=])[0-9a-f]{40}(?:[/?#]|$)")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# strings that identify a private machine or cluster location; allowed nowhere, also not under "provenance"
PRIVATE_PATH_RE = re.compile(r"/share/|/scratch/|/Users/|\.codex|/private/tmp|/home/|/mnt/")  # hygiene-allow
# the archive roots a "provenance" path starts from, written $NAME/... (the organizers keep the locations they
# stand for)
ARCHIVE_ROOTS = ("LCB_ARCHIVE", "LCB_TEAM_ARCHIVE")
ARCHIVE_ROOT_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)/")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode())


def module_of_path(path: str) -> str:
    return path.removesuffix(".lean").replace("/", ".")


class LintError:
    """One named lint failure: `code` is stable (tests and tools match on it), `detail` explains it."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"

    def __repr__(self) -> str:
        return f"LintError({self.code!r}, {self.detail!r})"


def read_explicit_background(spec: dict, base: Path) -> list[str]:
    """The explicit background module list (one module per line) named by background.explicit_modules."""
    return [line.strip() for line in (base / spec["content_file"]).read_text().splitlines() if line.strip()]


class ModuleClasses:
    """Module classes of a task: `editable` (charged, participant-owned), `fixed_charged` (read-only, charged:
    frozen modules, support modules) and `background` (free: a prefix list and/or an explicit module list).
    The charged set is editable + fixed_charged; the class of the core modules is stated in `core`."""

    def __init__(self, src: dict, base: Path):
        self.editable = list(src["owner_modules"])
        fixed = src.get("fixed_files", [])
        self.fixed_charged = [f["module"] for f in fixed if f.get("class") == "fixed_charged"]
        self.fixed_background = [f["module"] for f in fixed if f.get("class") == "background"]
        bg = src["background"]
        self.prefixes = list(bg.get("modules", []))
        explicit = bg.get("explicit_modules")
        self.explicit = set(read_explicit_background(explicit, base)) if explicit else set()
        self.charged = self.editable + self.fixed_charged

    def in_background(self, module: str) -> bool:
        return (module in self.explicit or module in self.fixed_background
                or any(module == p or module.startswith(p + ".") for p in self.prefixes))

    def classify(self, module: str) -> str:
        if module in self.editable:
            return "editable"
        if module in self.fixed_charged:
            return "fixed_charged"
        if self.in_background(module):
            return "background"
        return "unclassified"


def _private_paths(node, where: str, out: list[LintError], provenance: bool = False) -> None:
    """No private machine or cluster path anywhere. Under "provenance" a path names where the task came from relative
    to an archive root ($LCB_ARCHIVE/...), so the source itself holds no private path."""
    provenance = provenance or where == "$.provenance"
    if isinstance(node, dict):
        for k, v in node.items():
            _private_paths(v, f"{where}.{k}", out, provenance)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _private_paths(v, f"{where}[{i}]", out, provenance)
    elif isinstance(node, str):
        if PRIVATE_PATH_RE.search(node):
            code = "private_path_in_provenance" if provenance else "cluster_path_outside_provenance"
            out.append(LintError(code, f"{where}: {node[:120]!r}"))
        unknown = [r for r in ARCHIVE_ROOT_RE.findall(node) if r not in ARCHIVE_ROOTS]
        if provenance and unknown:
            out.append(LintError("unknown_archive_root", f"{where}: ${unknown[0]}/ is not one of "
                                 f"{', '.join('$' + r + '/' for r in ARCHIVE_ROOTS)}"))


def _content(base: Path, rel: str, want: str, what: str, out: list[LintError]) -> bytes | None:
    path = base / rel
    if not path.is_file():
        out.append(LintError("content_hash", f"{what}: content file {rel} is missing"))
        return None
    data = path.read_bytes()
    if sha256_bytes(data) != want:
        out.append(LintError("content_hash", f"{what}: {rel} sha256 {sha256_bytes(data)} != {want}"))
        return None
    return data


def _check_fields(src: dict, out: list[LintError]) -> bool:
    missing = [f for f in REQUIRED if f not in src and f != "source_license"]
    for f in missing:
        out.append(LintError("missing_field", f"required field {f!r} is missing"))
    lic = src.get("source_license")
    if not isinstance(lic, dict) or not str(lic.get("spdx", "")).strip():
        out.append(LintError("source_license_missing", "source_license.spdx must name the license of the task source "
                                                       "(SPDX id or LicenseRef-...)"))
    return not missing


def _check_revision(src: dict, out: list[LintError]) -> None:
    rev = src["task_revision"]
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        out.append(LintError("bad_value", f"task_revision must be an integer >= 1, got {rev!r}"))
        return
    history = src["revision_history"]
    if not isinstance(history, list) or not any(isinstance(h, dict) and h.get("revision") == rev for h in history):
        out.append(LintError("bad_value", f"revision_history has no entry for task_revision {rev}"))
    for h in history if isinstance(history, list) else []:
        if not isinstance(h, dict) or not DATE_RE.match(str(h.get("date", ""))) or not h.get("change"):
            out.append(LintError("bad_value", f"revision_history entry needs revision, date YYYY-MM-DD, change: {h!r}"))
    if src["status"] not in STATUSES:
        out.append(LintError("bad_value", f"status {src['status']!r} not in {sorted(STATUSES)}"))
    date = src["source_commit_date"]
    if date is not None and not DATE_RE.match(str(date)):
        out.append(LintError("bad_value", f"source_commit_date must be YYYY-MM-DD or null, got {date!r}"))


def _check_pins(src: dict, out: list[LintError]) -> None:
    tc = src["toolchain"]
    if not COMMIT_RE.match(str(tc.get("githash", ""))):
        out.append(LintError("not_pinned", f"toolchain.githash must be a 40-hex commit, got {tc.get('githash')!r}"))
    for p in src["project"].get("packages", []):
        if not COMMIT_RE.match(str(p.get("rev", ""))):
            out.append(LintError("not_pinned", f"package {p.get('name')} rev must be a 40-hex commit, got {p.get('rev')!r}"))
    tools = src["tools"]
    if not isinstance(tools, dict) or not tools:
        out.append(LintError("bad_value", "tools must map a tool name to {url, commit, role}"))
    else:
        for name, t in tools.items():
            if not isinstance(t, dict) or not COMMIT_RE.match(str(t.get("commit", ""))):
                out.append(LintError("not_pinned", f"tool {name} must be pinned by a 40-hex commit"))
    if src.get("commit") is not None and not COMMIT_RE.match(str(src["commit"])):
        out.append(LintError("not_pinned", f"commit must be a 40-hex commit, got {src['commit']!r}"))


def _check_limits(src: dict, out: list[LintError]) -> None:
    limits = src["limits"]
    if not isinstance(limits, dict):
        out.append(LintError("bad_value", "limits must be an object"))
        return
    for key in LIMIT_KEYS + OPTIONAL_LIMIT_KEYS:
        if key not in limits:
            if key in LIMIT_KEYS:
                out.append(LintError("missing_field", f"limits.{key} is missing"))
        elif not isinstance(limits[key], int) or isinstance(limits[key], bool) or limits[key] <= 0:
            out.append(LintError("bad_value", f"limits.{key} must be a positive integer, got {limits[key]!r}"))
    for key in sorted(set(limits) - set(LIMIT_KEYS) - set(OPTIONAL_LIMIT_KEYS)):
        why = ("a wall-clock limit would make validity depend on the machine; use kernel_heartbeats"
               if key in WALL_CLOCK_LIMIT_KEYS else "unknown limit")
        out.append(LintError("bad_value", f"limits.{key}: {why}"))


def _check_files(src: dict, base: Path, out: list[LintError]) -> None:
    lic = src.get("source_license")
    has_license = isinstance(lic, dict) and str(lic.get("spdx", "")).strip()
    proj = src["project"]
    for key in ("lakefile", "lake_manifest"):
        spec = proj.get(key)
        if spec:
            _content(base, spec["content_file"], spec["sha256"], f"project.{key}", out)
    for e in src["editable"]:
        if "content_file" in e:
            if has_license and lic.get("redistribution") != "vendored":
                out.append(LintError("vendored_without_license",
                                     f"{e['path']} is vendored but source_license.redistribution is "
                                     f"{lic.get('redistribution')!r}; use a fetch spec"))
            _content(base, e["content_file"], e["original_sha256"], f"editable {e['path']}", out)
        elif "fetch" in e:
            if not COMMIT_IN_URL_RE.search(e["fetch"].get("url", "")):
                out.append(LintError("not_pinned", f"editable {e['path']}: fetch url must contain a 40-hex commit"))
        else:
            out.append(LintError("missing_field", f"editable {e['path']} needs content_file or fetch"))
    if has_license and lic.get("redistribution") not in REDISTRIBUTION:
        out.append(LintError("bad_value", f"source_license.redistribution must be one of {sorted(REDISTRIBUTION)}"))
    for f in src.get("fixed_files", []):
        if f.get("class") not in FIXED_CLASSES:
            out.append(LintError("bad_value", f"fixed file {f.get('path')}: class must be one of {sorted(FIXED_CLASSES)}"))
        if module_of_path(f.get("path", "")) != f.get("module"):
            out.append(LintError("path_module_mismatch", f"fixed file {f.get('path')} is not module {f.get('module')}"))
        if "content_file" in f:
            _content(base, f["content_file"], f["sha256"], f"fixed file {f['path']}", out)
        elif not COMMIT_IN_URL_RE.search(f.get("fetch", {}).get("url", "")):
            out.append(LintError("not_pinned", f"fixed file {f.get('path')}: needs content_file or a pinned fetch url"))
    explicit = src["background"].get("explicit_modules")
    if explicit:
        data = _content(base, explicit["content_file"], explicit["sha256"], "background.explicit_modules", out)
        if data is not None:
            n = len([x for x in data.decode().splitlines() if x.strip()])
            if n != explicit.get("count"):
                out.append(LintError("bad_value", f"background.explicit_modules lists {n} modules, count says "
                                                  f"{explicit.get('count')}"))
    fm = src.get("frozen_module")
    if fm is not None and fm not in [f["module"] for f in src.get("fixed_files", []) if f.get("class") == "fixed_charged"]:
        out.append(LintError("bad_value", f"frozen_module {fm!r} must be a fixed_charged module in fixed_files"))


def _check_classes(src: dict, base: Path, out: list[LintError]) -> None:
    try:
        classes = ModuleClasses(src, base)
    except (OSError, KeyError) as e:
        out.append(LintError("bad_value", f"cannot read module classes: {e}"))
        return
    for m in classes.charged:
        if classes.in_background(m):
            out.append(LintError("editable_in_background", f"charged module {m} is matched by the background"))
    by_path = sorted(e.get("module") or module_of_path(e["path"]) for e in src["editable"])
    if by_path != sorted(classes.editable):
        out.append(LintError("path_module_mismatch", f"owner_modules {sorted(classes.editable)} != modules of the "
                                                     f"editable paths {by_path}"))
    dup = sorted({m for m in classes.charged if classes.charged.count(m) > 1})
    if dup:
        out.append(LintError("duplicate", f"module listed twice: {dup}"))
    core = src["core"]
    if not isinstance(core, dict) or core.get("class") not in ("background", "fixed_charged"):
        out.append(LintError("bad_value", "core must be {modules: [...], class: background|fixed_charged}"))
    elif core["class"] == "background":
        for m in core.get("modules", []):
            if m not in classes.prefixes:
                out.append(LintError("core_class_mismatch", f"core module {m} is declared background but is not a "
                                                            f"background prefix"))
    names = [t["name"] for t in src["targets"]]
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        out.append(LintError("duplicate", f"target listed twice: {dup}"))
    for t in src["targets"]:
        if t.get("module") not in classes.charged:
            out.append(LintError("target_module", f"target {t['name']} is in {t.get('module')}, which is not a "
                                                  f"charged module"))
    frozen = src["frozen"]
    if isinstance(frozen, dict):
        add = frozen.get("add")
        if (set(frozen) != {"rule", "add"} or frozen.get("rule") != "statement_closure" or not isinstance(add, list)
                or not all(isinstance(n, str) and n for n in add) or len(set(add)) != len(add)):
            out.append(LintError("bad_value", "frozen as an object must be {\"rule\": \"statement_closure\", \"add\": "
                                              "[distinct names]} (the statement closure plus named definitions)"))
    elif not (frozen == "statement_closure" or isinstance(frozen, list)):
        out.append(LintError("bad_value", "frozen must be \"statement_closure\", a list of {name, closure_sha256} or "
                                          "{\"rule\": \"statement_closure\", \"add\": [names]}"))


def _check_rules(src: dict, out: list[LintError]) -> None:
    metrics = src["metrics"]
    if metrics.get("primary") not in PRIMARY_METRICS:
        out.append(LintError("bad_value", f"metrics.primary {metrics.get('primary')!r} not in {sorted(PRIMARY_METRICS)}"))
    for m in metrics.get("recorded", []):
        if m not in KNOWN_METRICS:
            out.append(LintError("bad_value", f"metrics.recorded {m!r} is not a known metric id"))
    for m in src["baseline"]:
        if m not in KNOWN_METRICS:
            out.append(LintError("bad_value", f"baseline key {m!r} is not a known metric id"))
    if not set(src["modes"]) <= MODES or not src["modes"]:
        out.append(LintError("bad_value", f"modes must be a non-empty subset of {sorted(MODES)}"))
    if not src["rules"].get("allowed_axioms"):
        out.append(LintError("missing_field", "rules.allowed_axioms is missing"))


def lint(src: dict, base: Path) -> list[LintError]:
    """All problems of one task source (base = the directory holding task_source.json); [] means clean."""
    out: list[LintError] = []
    if src.get("schema") != SCHEMA:
        return [LintError("bad_value", f"schema must be {SCHEMA}")]
    if not _check_fields(src, out):
        return out
    _private_paths(src, "$", out)
    _check_revision(src, out)
    _check_pins(src, out)
    _check_limits(src, out)
    _check_files(src, base, out)
    _check_classes(src, base, out)
    _check_rules(src, out)
    return out


def lint_file(path: Path) -> list[LintError]:
    try:
        src = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        return [LintError("bad_value", f"cannot read {path}: {e}")]
    return lint(src, path.parent)


def task_sources(root: Path) -> list[Path]:
    """A task_source.json path, a task directory, or a directory of task directories."""
    if root.is_file():
        return [root]
    if (root / "task_source.json").is_file():
        return [root / "task_source.json"]
    return sorted(root.glob("*/task_source.json"))


def cmd_lint(args) -> int:
    paths = [p for root in args.paths for p in task_sources(Path(root))]
    if not paths:
        print("no task_source.json found")
        return 1
    bad = 0
    for p in paths:
        errors = lint_file(p)
        name = p.parent.name
        if errors:
            bad += 1
            for e in errors:
                print(f"{name}: {e}")
        else:
            print(f"{name}: ok")
    return 1 if bad else 0


def _n(value) -> str:
    return "-" if value is None else f"{value:,}"


def frozen_description(frozen) -> str:
    """The frozen-set row of a task card."""
    if frozen == "statement_closure":
        return "mechanical (statement closure)"
    if isinstance(frozen, dict):
        names = ", ".join(f"`{n}`" for n in frozen["add"])
        return (f"mechanical (statement closure) plus {len(frozen['add'])} named "
                f"definition{'' if len(frozen['add']) == 1 else 's'}" + (f": {names}" if names else ""))
    return f"hand list of {len(frozen)} names, checked to contain the mechanical set"


def render_card(src: dict, base: Path, measured: dict | None) -> str:
    """tasks/<id>/CARD.md: the per-task card (source, license, module classes, targets, baselines, measurements).
    Everything is derived from task_source.json and tasks/<id>/measured.json, so the card cannot drift."""
    classes = ModuleClasses(src, base)
    lic = src["source_license"]
    bg = src["background"]
    explicit = bg.get("explicit_modules")
    frozen = src["frozen"]
    origin = (f"{src['repository']} @ `{src['commit']}`" if src.get("repository") else "written for lcbench")
    fetched = sum("fetch" in e for e in src["editable"])
    lines = [f"# {src['task_id']}: {src['title']}", "",
             "<!-- generated by `lcbench tasks card`; edit task_source.json or measured.json instead -->", "",
             "| field | value |", "|---|---|",
             f"| task id / revision / status | `{src['task_id']}` / {src['task_revision']} / {src['status']} |",
             f"| source | {origin} (commit date {src['source_commit_date'] or '-'}) |",
             f"| license | {lic['spdx']} ({lic['redistribution']}; {len(src['editable']) - fetched} vendored, "
             f"{fetched} fetched by URL@commit + sha256) |",
             f"| toolchain | {src['toolchain']['id']} (`{src['toolchain']['githash'][:12]}`) |",
             f"| Lake packages | {', '.join(p['name'] + '@' + p['rev'][:8] for p in src['project'].get('packages', [])) or 'none'} |",
             f"| editable modules | {len(classes.editable)}: {', '.join(f'`{m}`' for m in classes.editable)} |",
             f"| fixed_charged modules | {len(classes.fixed_charged)}"
             + (": " + ", ".join(f"`{m}`" for m in classes.fixed_charged) if classes.fixed_charged else "") + " |",
             f"| background | prefixes {', '.join(f'`{m}`' for m in bg.get('modules', []))}"
             + (f"; explicit list of {explicit['count']:,} modules" if explicit else "")
             + (f"; {len(classes.fixed_background)} fixed background modules" if classes.fixed_background else "")
             + f"; core {', '.join(src['core']['modules'])} = {src['core']['class']} |",
             f"| targets | {len(src['targets'])} theorems |",
             f"| frozen set | {frozen_description(frozen)} |",
             *([f"| upstream solution module | `{src['comparator']['config']['solution_module']}` (build it in the "
                f"upstream checkout to prepare the Lake packages) |"]
               if isinstance(src.get("comparator"), dict) and src["comparator"].get("config", {}).get("solution_module")
               else []),
             f"| modes | {', '.join(src['modes'])} (official: warm) |",
             f"| limits | kernel {src['limits']['kernel_heartbeats']:,} heartbeats per declaration (the only "
             f"validity limit); safety limits that give no verdict: memory {src['limits']['mem_mb']:,} MB, wall clock "
             f"{src['limits'].get('safety_wall_s', 21600):,} s |",
             f"| baseline proof-term node count (`expr_nodes_with_levels_v1`) | {_n(src['baseline'].get('expr_nodes_with_levels_v1'))} |",
             ""]
    if measured:
        where = (f"Job {measured['job']} on {measured['node']} ({measured['platform']}), lcbench commit "
                 f"`{measured['repo_commit'][:12]}`:" if "job" in measured else f"On {measured['platform']}:")
        lines += ["## Measured", "", where, "",
                  "| step | wall time |",
                  "|---|---|",
                  *[f"| {step} | {secs:,.0f} s |" for step, secs in measured["seconds"].items()],
                  "",
                  f"Peak memory of the job: {measured['peak_rss_mb']:,} MB "
                  f"({measured.get('peak_rss_source') or 'source not recorded'}). ./check node count = "
                  f"{_n(measured['A'])} ({'exact' if measured['A'] == src['baseline'].get('expr_nodes_with_levels_v1') else 'MISMATCH'}).",
                  f"Warm tarball sha256 `{measured['tarball_sha256']['warm']}`"
                  + (f"; no_hint `{measured['tarball_sha256']['no_hint']}`." if "no_hint" in measured["tarball_sha256"]
                     else "."),
                  ""]
        if measured.get("dag_nodes_v1") is not None:
            lines += [f"dag_nodes_v1 = {measured['dag_nodes_v1']:,} (recorded, not ranked).", ""]
        prov = measured.get("provisioning")
        if prov:
            disk = prov["disk_mb"]
            lines += ["### Setup on a clean machine", "",
                      (f"Job {prov['job']} on {prov['node']} " if "job" in prov else "On ")
                      + f"({prov['platform']}): `lcbench start` without `--deps` "
                      f"(Lake clones the pinned packages, `lake exe cache get` downloads Mathlib) took "
                      f"{prov['start_seconds']:,.0f} s and {disk['project_lake']:,} MB in `project/.lake` "
                      f"(+{disk['home_cache']:,} MB of caches under HOME); the first ./check took "
                      f"{_n(prov.get('check_seconds'))} s and gave a node count of {_n(prov.get('A'))}; peak memory of the "
                      f"job {_n(prov.get('peak_rss_mb'))} MB. Not measured on macOS or arm64.", ""]
    else:
        lines += ["## Measured", "", "Not measured yet.", ""]
    if src.get("notes"):
        lines += ["## Notes", "", src["notes"], ""]
    return "\n".join(lines)


def load_measured(task_dir: Path) -> dict | None:
    p = task_dir / "measured.json"
    return json.loads(p.read_text()) if p.is_file() else None


def cmd_card(args) -> int:
    for p in [p for root in args.paths for p in task_sources(Path(root))]:
        src = json.loads(p.read_text())
        card = render_card(src, p.parent, load_measured(p.parent))
        if args.check:
            if not (p.parent / "CARD.md").is_file() or (p.parent / "CARD.md").read_text() != card:
                print(f"{p.parent.name}: CARD.md is stale (run mcb tasks card {p.parent})")
                return 1
        else:
            (p.parent / "CARD.md").write_text(card)
        print(f"{p.parent.name}: CARD.md {'up to date' if args.check else 'written'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcb tasks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("lint", help="lint task sources (lcbench.task_source.v1)")
    p.add_argument("paths", nargs="+", help="tasks/ directory, a task directory or a task_source.json")
    p = sub.add_parser("card", help="write tasks/<id>/CARD.md from task_source.json and measured.json")
    p.add_argument("paths", nargs="+", help="tasks/ directory, a task directory or a task_source.json")
    p.add_argument("--check", action="store_true", help="fail if a committed CARD.md differs from the rendering")
    args = ap.parse_args(argv)
    return {"lint": cmd_lint, "card": cmd_card}[args.cmd](args)


COMMANDS = {"tasks": ("task sources: `tasks lint tasks/` checks them, `tasks card tasks/` writes CARD.md", main)}
