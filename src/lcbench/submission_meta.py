# ABOUTME: A submission's metadata.yaml: the leaderboard tracks, the required fields, a small YAML reader (standard library
# ABOUTME: only) and the validator. Self-contained, so the submissions repository and the intake run the same file.
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The leaderboard tracks and the wall-clock limit of one run in seconds (None: no limit). `open` is the main track;
# the two agent tracks are the study's controlled settings. An agent-track result also qualifies for `open`.
TRACKS = {"open": None, "agent-60": 3600, "agent-120": 7200}
MAIN_TRACK = "open"
REQUIRED = ("system", "model", "harness", "reasoning_effort", "track", "human_assisted", "tasks", "attempts_per_task",
            "contact")
OPTIONAL = ("links", "logs")
# what each field holds, for the problems a participant reads
MEANING = {
    "system": "the name of your system, as the leaderboard shows it",
    "model": "the model and its version, for example a model id with its date or release",
    "harness": "the program that ran the model, with its version",
    "reasoning_effort": "the reasoning effort the model was run with (for example high), or none",
    "track": "one of " + ", ".join(TRACKS),
    "human_assisted": "true or false: whether a person helped (only the open track allows true)",
    "tasks": "the list of tasks this submission covers, for example [toyarith-warm]",
    "attempts_per_task": "how many attempts you made per task, all of them, not only the submitted one: a whole "
                         "number, or one number per task",
    "contact": "how the organizers reach you (an email address); it is not published",
    "links": "optional: a list of links (an article, code, a write-up)",
    "logs": "optional: a link to your run logs, or a list of links",
}
# The submissions repository holds one metadata.yaml per submission: entries/<entry>/metadata.yaml.
ENTRY_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
ENTRY_PATH = re.compile(r"entries/([a-z0-9][a-z0-9_.-]{0,63})/metadata\.yaml")
TASK_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]*")
MAX_METADATA_YAML_BYTES = 64 << 10
# YAML words that some readers take for true or false; the metadata asks for plain true or false instead
AMBIGUOUS_BOOLEANS = {"yes", "no", "on", "off", "y", "n", "True", "False", "TRUE", "FALSE", "Yes", "No", "YES", "NO",
                      "On", "Off", "ON", "OFF"}
PLAIN_INT = re.compile(r"[-+]?\d+")


class MetadataError(ValueError):
    """metadata.yaml is not in the YAML subset this reader takes; the message names the line."""


# ---------------------------------------------------------------- the YAML subset

def _strip_comment(line: str) -> str:
    """The line without a comment: `#` at the start or after a space, outside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or line[i - 1] in " [,:"):
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
    return line.rstrip()


def _scalar(text: str, where: str):
    """One YAML scalar: a quoted string, true, false, null, a whole number, or a plain string."""
    text = text.strip()
    if text == "":
        return None
    if text[0] == '"':
        if len(text) < 2 or not text.endswith('"'):
            raise MetadataError(f"{where}: unclosed double quote")
        try:
            return json.loads(text)
        except ValueError as e:
            raise MetadataError(f"{where}: bad double-quoted string ({e})") from None
    if text[0] == "'":
        if len(text) < 2 or not text.endswith("'"):
            raise MetadataError(f"{where}: unclosed single quote")
        inner = text[1:-1]
        if re.search(r"(?<!')'(?!')", inner):
            raise MetadataError(f"{where}: a single quote inside a single-quoted string is written ''")
        return inner.replace("''", "'")
    if text in ("true", "false"):
        return text == "true"
    if text in ("null", "~"):
        return None
    if text in AMBIGUOUS_BOOLEANS:
        raise MetadataError(f"{where}: write true or false (or quote the word), not {text}")
    if PLAIN_INT.fullmatch(text):
        return int(text)
    if text[0] in "[]{}&*!|>%@`,?-" and text != "-":
        raise MetadataError(f"{where}: put this value in double quotes: {text}")
    if ": " in text or text.endswith(":"):
        raise MetadataError(f"{where}: a value holding ': ' must be in double quotes: {text}")
    return text


def _split_flow(inner: str, where: str) -> list[str]:
    """The items of a flow list's inside, split at commas outside quotes."""
    items, quote, start = [], None, 0
    for i, ch in enumerate(inner):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "[]{}":
            raise MetadataError(f"{where}: nested lists or mappings are not supported here")
        elif ch == ",":
            items.append(inner[start:i])
            start = i + 1
    if quote:
        raise MetadataError(f"{where}: unclosed quote")
    items.append(inner[start:])
    if len(items) == 1 and not items[0].strip():
        return []
    if any(not s.strip() for s in items):
        raise MetadataError(f"{where}: empty item in the list")
    return items


def _value(text: str, where: str):
    if text.startswith("["):
        if not text.endswith("]"):
            raise MetadataError(f"{where}: a list written [a, b] must end with ]")
        return [_scalar(s, where) for s in _split_flow(text[1:-1], where)]
    if text.startswith("{"):
        raise MetadataError(f"{where}: write a mapping as indented `key: value` lines, not {{...}}")
    return _scalar(text, where)


KEY_LINE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)\s*:(?:\s+(.*))?")


def parse_yaml(text: str) -> dict:
    """The YAML subset metadata.yaml uses: top-level `key: value` lines; a value is a scalar (a quoted or plain
    string, true, false, null or a whole number), a list `[a, b]` or indented `- item` lines, or one level of indented
    `key: value` lines. Anything else is refused with its line number rather than read differently from YAML."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    out: dict = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        where = f"line {i + 1}"
        if "\t" in raw[:len(raw) - len(raw.lstrip())]:
            raise MetadataError(f"{where}: indent with spaces, not tabs")
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            i += 1
            continue
        if line[0] == " ":
            raise MetadataError(f"{where}: unexpected indentation (top-level keys start in the first column)")
        m = KEY_LINE.fullmatch(line)
        if not m:
            raise MetadataError(f"{where}: expected `key: value`, got: {line.strip()}")
        key, rest = m.group(1), (m.group(2) or "").strip()
        if key in out:
            raise MetadataError(f"{where}: {key} is given twice")
        i += 1
        if rest:
            out[key] = _value(rest, where)
            continue
        block: list[tuple[int, str]] = []
        while i < len(lines):
            body = _strip_comment(lines[i])
            if body.strip() and body[0] != " ":
                break
            if "\t" in lines[i][:len(lines[i]) - len(lines[i].lstrip())]:
                raise MetadataError(f"line {i + 1}: indent with spaces, not tabs")
            if body.strip():
                block.append((i + 1, body.strip()))
            i += 1
        if not block:
            out[key] = None
        elif all(t == "-" or t.startswith("- ") for _, t in block):
            items = []
            for n, t in block:
                item = t[1:].strip()
                if KEY_LINE.fullmatch(item):
                    raise MetadataError(f"line {n}: list items must be plain values, not `key: value`")
                items.append(_value(item, f"line {n}") if item else None)
            out[key] = items
        elif all(not t.startswith("-") for _, t in block):
            mapping: dict = {}
            for n, t in block:
                mm = KEY_LINE.fullmatch(t)
                if not mm or not (mm.group(2) or "").strip():
                    raise MetadataError(f"line {n}: expected `key: value` under {key}")
                if mm.group(1) in mapping:
                    raise MetadataError(f"line {n}: {mm.group(1)} is given twice under {key}")
                mapping[mm.group(1)] = _value(mm.group(2).strip(), f"line {n}")
            out[key] = mapping
        else:
            raise MetadataError(f"line {block[0][0]}: {key} mixes `- item` lines with `key: value` lines")
    return out


# ---------------------------------------------------------------- the rules

def _text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def metadata_problems(meta, known_tasks=None) -> list[str]:
    """Every reason this metadata is incomplete or wrong; [] when it may be reviewed. `known_tasks` (optional) are the
    task names the organizers accept."""
    if not isinstance(meta, dict):
        return ["metadata.yaml must hold `key: value` lines (a mapping)"]
    problems = []
    for key in REQUIRED:
        if key not in meta or meta[key] is None or meta[key] == "" or meta[key] == []:
            problems.append(f"{key} is missing: {MEANING[key]}")
    for key in sorted(set(meta) - set(REQUIRED) - set(OPTIONAL)):
        problems.append(f"{key} is not a metadata field (the fields are {', '.join(REQUIRED + OPTIONAL)})")
    for key in ("system", "model", "harness", "reasoning_effort", "contact"):
        if meta.get(key) not in (None, "") and not _text(meta[key]):
            problems.append(f"{key} must be text: {MEANING[key]}")
    track = meta.get("track")
    if track is not None and track not in TRACKS:
        problems.append(f"track {track!r} is not a track: {MEANING['track']}")
    assisted = meta.get("human_assisted")
    if assisted is not None and not isinstance(assisted, bool):
        problems.append(f"human_assisted must be true or false, not {assisted!r}")
    if assisted is True and track in TRACKS and TRACKS[track] is not None:
        problems.append(f"track {track} is for agents alone: human_assisted must be false (a human-assisted result "
                        f"goes on the {MAIN_TRACK} track)")
    tasks = meta.get("tasks")
    names: list[str] = []
    if tasks not in (None, []):
        if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
            problems.append(f"tasks must be a list of task names: {MEANING['tasks']}")
        else:
            names = tasks
            bad = [t for t in tasks if not TASK_NAME.fullmatch(t)]
            if bad:
                problems.append(f"tasks: not a task name: {', '.join(bad)}")
            twice = sorted({t for t in tasks if tasks.count(t) > 1})
            if twice:
                problems.append(f"tasks: listed twice: {', '.join(twice)}")
            if known_tasks is not None:
                unknown = [t for t in tasks if TASK_NAME.fullmatch(t) and t not in known_tasks]
                if unknown:
                    problems.append(f"tasks: the organizers do not accept {', '.join(unknown)} (open tasks: "
                                    f"{', '.join(sorted(known_tasks))})")
    attempts = meta.get("attempts_per_task")
    if attempts is not None:
        if isinstance(attempts, dict):
            if not all(_count(v) for v in attempts.values()):
                problems.append("attempts_per_task: every number must be a whole number of at least 1")
            if names and set(attempts) != set(names):
                problems.append("attempts_per_task must give one number for each task in tasks, and no other "
                                f"(tasks: {', '.join(names)}; given: {', '.join(attempts)})")
        elif not _count(attempts):
            problems.append(f"attempts_per_task must be a whole number of at least 1, or one per task, not "
                            f"{attempts!r}")
    links = meta.get("links")
    if links is not None and not (isinstance(links, list) and all(_text(x) for x in links)):
        problems.append("links must be a list of links")
    logs = meta.get("logs")
    if logs is not None and not (_text(logs) or (isinstance(logs, list) and all(_text(x) for x in logs))):
        problems.append("logs must be a link or a list of links")
    return problems


def attempts_for(meta: dict, task: str) -> int | None:
    """The declared number of attempts on `task`."""
    a = meta.get("attempts_per_task")
    if isinstance(a, dict):
        return a.get(task)
    return a if _count(a) else None


def run_problems(track: str, run_track, budget_seconds) -> list[str]:
    """Why a run does not qualify for `track`: an agent track needs a run started with `lcbench start --track agent`
    and a time budget of at most the track's limit. `run_track` and `budget_seconds` are what the bundle's
    SUBMISSION.json (metadata.track, metadata.run.budget_seconds) or the organizers' receipt (track,
    run.budget_seconds) record. The open track takes any run."""
    limit = TRACKS.get(track)
    if limit is None:
        return []
    problems = []
    if run_track != "agent":
        problems.append(f"the {track} track is for agent runs: start the workspace with `lcbench start --track agent` "
                        f"(the run records track {run_track!r})")
    if not (isinstance(budget_seconds, int) and not isinstance(budget_seconds, bool) and 0 < budget_seconds <= limit):
        problems.append(f"the {track} track allows at most {limit} seconds per run: start the workspace with "
                        f"`--budget-seconds {limit}` or less (the run records a budget of {budget_seconds!r})")
    return problems


def system_key(meta: dict) -> tuple:
    """Submissions that agree on these fields are one system on the leaderboard: its best final per task counts."""
    def norm(v):
        return " ".join(str(v).split()).casefold()
    return (norm(meta.get("system")), norm(meta.get("model")), norm(meta.get("harness")),
            norm(meta.get("reasoning_effort")), bool(meta.get("human_assisted")))


def load(path: Path, known_tasks=None) -> tuple[dict | None, list[str]]:
    """(metadata, problems) of a metadata.yaml file."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, [f"{path}: cannot be read ({type(e).__name__})"]
    return loads(data, known_tasks)


def loads(data: bytes, known_tasks=None) -> tuple[dict | None, list[str]]:
    if len(data) > MAX_METADATA_YAML_BYTES:
        return None, [f"metadata.yaml is {len(data)} bytes, more than the limit of {MAX_METADATA_YAML_BYTES}"]
    try:
        meta = parse_yaml(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None, ["metadata.yaml must be UTF-8 text"]
    except MetadataError as e:
        return None, [f"metadata.yaml is not readable: {e}"]
    return meta, metadata_problems(meta, known_tasks)


# ---------------------------------------------------------------- command line

def open_tasks_near(path: Path) -> set | None:
    """The open tasks of the submissions checkout that holds `path` (its accepted-packages.json), if any."""
    for parent in [path, *path.parents]:
        listing = parent / "accepted-packages.json"
        if listing.is_file():
            try:
                return set(json.loads(listing.read_text())["tasks"])
            except (ValueError, KeyError, OSError):
                return None
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="lcbench submission",
                                 description="Check a submission's metadata.yaml before you open the pull request.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("validate", help="check metadata.yaml: every required field, the track rules, the tasks")
    p.add_argument("path", type=Path, help="the entry folder (entries/<entry>/) or its metadata.yaml")
    p.add_argument("--tasks", help="comma-separated task names the organizers accept (default: the checkout's "
                                   "accepted-packages.json, when there is one)")
    args = ap.parse_args(argv)
    path = args.path / "metadata.yaml" if args.path.is_dir() else args.path
    known = set(args.tasks.split(",")) if args.tasks else open_tasks_near(path.resolve().parent)
    meta, problems = load(path, known)
    if problems:
        print(f"NOT OK: {path} would not be reviewed ({len(problems)} problem(s)):")
        for problem in problems:
            print(f"- {problem}")
        return 1
    tasks = ", ".join(meta["tasks"])
    print(f"OK: {path}: {meta['system']} on the {meta['track']} track"
          f"{' (human-assisted)' if meta['human_assisted'] else ''}, tasks {tasks}")
    if known is None:
        print("note: no accepted-packages.json found, so whether the tasks are open was not checked")
    return 0


COMMANDS = {"submission": ("participant: check a submission's metadata.yaml (validate)", main)}


if __name__ == "__main__":
    sys.exit(main())
