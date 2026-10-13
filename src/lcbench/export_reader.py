# ABOUTME: Reads an lcbench.metric_export.v1 NDJSON file (the canonical hash-consed export written by lcbench-metric)
# ABOUTME: and recomputes expr_nodes_with_levels_v1 and dag_nodes_v1 from it, independently of the Lean counter.
from __future__ import annotations

import argparse
import json
import sys

SCHEMA = "lcbench.metric_export.v1"

# children of each expression kind, by field name, in the export's order
EXPR_CHILDREN = {"bv": (), "so": (), "c": (), "ap": ("f", "a"), "la": ("t", "b"), "pi": ("t", "b"),
                 "le": ("t", "v", "b"), "nl": (), "sl": (), "pj": ("x",)}


class ExportError(ValueError):
    """The export is malformed: unknown record, forward or dangling reference, or an out-of-order id."""


def _need(table: dict, key, what: str, line_no: int):
    if key not in table:
        raise ExportError(f"line {line_no}: {what} {key!r} is not defined before use")
    return table[key]


def _next_id(new_id, table: dict, what: str, line_no: int) -> None:
    """Ids are dense and increase in file order, so children always have smaller ids than their parents."""
    if new_id != len(table):
        raise ExportError(f"line {line_no}: {what} id {new_id!r} out of order (expected {len(table)})")


def read_export(path) -> dict:
    """Parse the export into plain tables. Every reference must point to an earlier record."""
    names: dict[int, str] = {0: ""}
    levels: dict[int, dict] = {}
    exprs: dict[int, dict] = {}
    decls: list[dict] = []
    header = None
    with open(path, encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            rec = json.loads(line)
            if line_no == 1:
                if rec.get("schema") != SCHEMA:
                    raise ExportError(f"line 1: schema is {rec.get('schema')!r}, expected {SCHEMA!r}")
                header = rec
            elif "n" in rec and "p" in rec:
                _next_id(rec["n"], names, "name", line_no)
                pre = _need(names, rec["p"], "name", line_no)
                part = rec["s"] if "s" in rec else str(rec["i"])
                names[rec["n"]] = part if pre == "" else f"{pre}.{part}"
            elif "l" in rec:
                _next_id(rec["l"], levels, "level", line_no)
                for key in ("a", "b"):
                    if key in rec:
                        _need(levels, rec[key], "level", line_no)
                if rec["k"] == "p":
                    _need(names, rec["n"], "name", line_no)
                elif rec["k"] not in ("z", "s", "m", "im"):
                    raise ExportError(f"line {line_no}: unknown level kind {rec['k']!r}")
                levels[rec["l"]] = rec
            elif "e" in rec:
                _next_id(rec["e"], exprs, "expression", line_no)
                kind = rec["k"]
                if kind not in EXPR_CHILDREN:
                    raise ExportError(f"line {line_no}: unknown expression kind {kind!r}")
                for key in EXPR_CHILDREN[kind]:
                    _need(exprs, rec[key], "expression", line_no)
                if kind == "so":
                    _need(levels, rec["u"], "level", line_no)
                if kind == "c":
                    _need(names, rec["n"], "name", line_no)
                    for u in rec["us"]:
                        _need(levels, u, "level", line_no)
                if kind == "pj":
                    _need(names, rec["s"], "name", line_no)
                exprs[rec["e"]] = rec
            elif "d" in rec:
                for key in ("type", "value"):
                    if rec.get(key) is not None:
                        _need(exprs, rec[key], "expression", line_no)
                for _ctor, _nfields, rhs in rec["rules"]:
                    _need(exprs, rhs, "expression", line_no)
                if rec["role"] not in ("target", "helper"):
                    raise ExportError(f"line {line_no}: unknown role {rec['role']!r}")
                decls.append(rec)
            else:
                raise ExportError(f"line {line_no}: unknown record {sorted(rec)}")
    if header is None:
        raise ExportError("empty export")
    return {"header": header, "names": names, "levels": levels, "exprs": exprs, "decls": decls}


def charged_exprs(decl: dict) -> list[int]:
    """A target pays its value; any other charged declaration pays type + value + recursor rule right-hand sides."""
    if decl["role"] == "target":
        return [decl["value"]] if decl["value"] is not None else []
    out = [decl["type"]] + ([decl["value"]] if decl["value"] is not None else [])
    return out + [rhs for _ctor, _nfields, rhs in decl["rules"]]


def level_sizes(levels: dict) -> dict[int, int]:
    size: dict[int, int] = {}
    for lid in sorted(levels):
        rec = levels[lid]
        size[lid] = 1 + sum(size[rec[key]] for key in ("a", "b") if key in rec)
    return size


def tree_sizes(exprs: dict, lsize: dict) -> dict[int, int]:
    """Tree size of every expression: one node per constructor, universe level nodes included, mdata free."""
    size: dict[int, int] = {}
    for eid in sorted(exprs):
        rec = exprs[eid]
        kind = rec["k"]
        n = 1 + sum(size[rec[key]] for key in EXPR_CHILDREN[kind])
        if kind == "so":
            n += lsize[rec["u"]]
        elif kind == "c":
            n += sum(lsize[u] for u in rec["us"])
        size[eid] = n
    return size


def dag_nodes(data: dict) -> int:
    """Distinct expression nodes reachable from the charged expressions, re-hash-consed from the export's fields
    (so duplicate records for the same node are counted once)."""
    exprs, levels, names = data["exprs"], data["levels"], data["names"]
    reach = set()
    for decl in data["decls"]:
        reach.update(charged_exprs(decl))
    for eid in sorted(exprs, reverse=True):
        if eid in reach:
            rec = exprs[eid]
            reach.update(rec[key] for key in EXPR_CHILDREN[rec["k"]])
    # Re-hash-cons bottom-up. Every key holds only small canonical ids of its children, never nested keys, so
    # hashing is constant time per node however deep the sharing goes.
    lkeys: dict[tuple, int] = {}
    lcanon: dict[int, int] = {}
    for lid in sorted(levels):
        rec = levels[lid]
        if rec["k"] == "p":
            key = ("p", names[rec["n"]])
        else:
            key = (rec["k"],) + tuple(lcanon[rec[k]] for k in ("a", "b") if k in rec)
        lcanon[lid] = lkeys.setdefault(key, len(lkeys))
    ekeys: dict[tuple, int] = {}
    canon: dict[int, int] = {}
    for eid in sorted(reach):
        rec = exprs[eid]
        kind = rec["k"]
        children = tuple(canon[rec[k]] for k in EXPR_CHILDREN[kind])
        if kind == "bv":
            key = ("bv", rec["i"])
        elif kind == "so":
            key = ("so", lcanon[rec["u"]])
        elif kind == "c":
            key = ("c", names[rec["n"]], tuple(lcanon[u] for u in rec["us"]))
        elif kind in ("la", "pi"):
            key = (kind, rec["bi"]) + children
        elif kind == "le":
            key = ("le", bool(rec["nd"])) + children
        elif kind in ("nl", "sl"):
            key = (kind, rec["v"])
        elif kind == "pj":
            key = ("pj", names[rec["s"]], rec["i"]) + children
        else:
            key = (kind,) + children
        canon[eid] = ekeys.setdefault(key, len(ekeys))
    return len(ekeys)


def recount(path) -> dict:
    """Recompute A (expr_nodes_with_levels_v1) and dag_nodes_v1 from an export file."""
    data = read_export(path)
    size = tree_sizes(data["exprs"], level_sizes(data["levels"]))
    per_decl = {decl["name"]: sum(size[e] for e in charged_exprs(decl)) for decl in data["decls"]}
    return {"expr_nodes_with_levels_v1": sum(per_decl.values()), "dag_nodes_v1": dag_nodes(data),
            "targets": sum(1 for d in data["decls"] if d["role"] == "target"),
            "helpers": sum(1 for d in data["decls"] if d["role"] == "helper"),
            "declarations": per_decl}


def metric_from_export(export: bytes) -> dict:
    """recount() of an export held in memory (a bundle's kernel_export.jsonl)."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".ndjson") as handle:
        handle.write(export)
        handle.flush()
        return recount(handle.name)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="lcbench export-recount",
                                 description="Recompute the proof-term node count and dag_nodes_v1 from an lcbench.metric_export.v1 file.")
    ap.add_argument("export", help="NDJSON export written by lcbench-metric")
    ap.add_argument("--per-declaration", action="store_true", help="also print each declaration's node count")
    args = ap.parse_args(argv)
    try:
        result = recount(args.export)
    except (ExportError, KeyError, json.JSONDecodeError) as err:
        print(json.dumps({"error": f"{type(err).__name__}: {err}"}))
        return 1
    if not args.per_declaration:
        result.pop("declarations")
    print(json.dumps(result, sort_keys=True))
    return 0


COMMANDS = {"export-recount": ("recompute the node count and dag_nodes_v1 from a metric export (Python cross-check)", main)}


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
