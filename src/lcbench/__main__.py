# ABOUTME: `lcbench` command line: dispatches `lcbench <command> ...` to the module that registered the command.
# ABOUTME: Each area lists its modules in AREA_MODULES (one line each); a module registers COMMANDS = {name: (help, main)}.
from __future__ import annotations

import importlib
import sys

# One line per module, grouped by area. Add a line inside the group of the module's area; the blank lines between
# groups keep parallel branches mergeable.
AREA_MODULES = [
    # area: verifier
    "lcbench.participant",
    "lcbench.verify",
    "lcbench.tlog",
    "lcbench.verify_receipt",

    # area: metric
    "lcbench.export_reader",
    "lcbench.metric",

    # area: tasks
    "lcbench.make_task",
    "lcbench.taskspec",
    "lcbench.package_audit",

    # area: intake
    "lcbench.intake_participant",
    "lcbench.submission_meta",
]


def commands(modules: list[str] | None = None) -> dict:
    """Collect {command: (help, main)} from every registered module; a command may be registered only once."""
    table: dict = {}
    for name in AREA_MODULES if modules is None else modules:
        for cmd, entry in importlib.import_module(name).COMMANDS.items():
            if cmd in table:
                raise RuntimeError(f"command {cmd!r} registered twice (again by {name})")
            table[cmd] = entry
    return table


def usage(table: dict) -> str:
    width = max(len(cmd) for cmd in table)
    lines = ["usage: mcb <command> [args...]   (mcb <command> --help for its options)", "", "commands:"]
    lines += [f"  {cmd.ljust(width)}  {help_text}" for cmd, (help_text, _) in sorted(table.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    table = commands()
    if not argv or argv[0] in ("-h", "--help"):
        print(usage(table))
        return 0 if argv else 2
    entry = table.get(argv[0])
    if entry is None:
        print(f"unknown command {argv[0]!r}\n{usage(table)}")
        return 2
    return entry[1](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
