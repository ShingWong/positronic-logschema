# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""`logschema` command line.

The help documents are the product, and they ship inside the package. An
agent reads them at the start of a session, so a doc that lives in the repo
but not in the wheel is a doc the installed tool cannot read.

Output contract, because this has to work on an agent harness we do not
control: plain text by default, `--json` where a machine consumer needs it,
and never a blocking prompt unless `--interactive` was passed. A harness that
does not wire stdin must get a question and an exit code, not a hang.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib import resources
from pathlib import Path

from .manifest import DIRECTORIES, MANIFEST

# Every verb this tool will ever have, with its readiness. The registry is the
# allow-list that `tests/test_help.py` checks the help documents against, so a
# document cannot name a command that does not exist. `logschema help` prints
# this table, which is why an agent can tell what is real from what is planned.
VERBS: list[tuple[str, str, str]] = [
    # (verb, status, one line)
    ("help", "ready", "basic help, or agent-startup / agent-detail"),
    ("project init", "ready", "create a project directory and write the manifest"),
    ("inspect", "ready", "field inventory, clock, identity candidates, classes, questions"),
    ("validate", "ready", "the six predicates; exit 0 only if all pass"),
    ("conformance write", "ready", "generate the negative fixtures"),
    ("conformance run", "ready", "run them; every one must be rejected"),
    ("doctor", "planned", "environment and version check"),
    ("preview", "planned", "the first N normalized events, no brain required"),
    ("draft", "agent", "candidate schema from prior knowledge (the agent writes this)"),
    ("revise", "agent", "revised candidate given a failure report (the agent writes this)"),
    ("schema", "planned", "list, show or diff the schema library"),
]

TOP_LEVEL = sorted({v.split()[0] for v, _, _ in VERBS})


def _help_text(topic: str) -> str:
    """Read a packaged help document. Raises FileNotFoundError if absent."""
    return (resources.files("logschema") / "help" / topic).read_text(encoding="utf-8")


def _brief() -> str:
    ready = [(v, s) for v, st, s in VERBS if st == "ready"]
    planned = [(v, st) for v, st, _ in VERBS if st != "ready"]
    lines = [
        "logschema — build and validate log-ingestion schemas.",
        "",
        "Standalone. No memory substrate required. Positronic is one consumer",
        "of the output, not this tool's identity.",
        "",
        "ready now:",
    ]
    lines += [f"  logschema {v:<18} {s}" for v, s in ready]
    lines += ["", "not yet implemented:"]
    lines += [f"  logschema {v:<18} ({st})" for v, st in planned]
    lines += [
        "",
        "topics:  logschema help agent-startup   bootstrap a new project",
        "         logschema help agent-detail     the build loop in full",
    ]
    return "\n".join(lines)


def _cmd_help(args: argparse.Namespace) -> int:
    if args.topic:
        try:
            print(_help_text(f"{args.topic}.md"))
        except (FileNotFoundError, ModuleNotFoundError):
            print(f"logschema: no help topic {args.topic!r}. "
                  f"Known topics: agent-startup, agent-detail", file=sys.stderr)
            return 2
        return 0
    print(_brief())
    return 0


def _cmd_project_init(args: argparse.Namespace) -> int:
    root = Path(args.location).expanduser().resolve() / args.name
    if root.exists() and any(root.iterdir()) and not args.force:
        print(f"logschema: {root} exists and is not empty. Use --force to "
              f"write into it anyway.", file=sys.stderr)
        return 2
    root.mkdir(parents=True, exist_ok=True)
    written = []
    for rel, purpose in MANIFEST:
        path = root / rel.rstrip("/")
        if rel in DIRECTORIES:
            path.mkdir(parents=True, exist_ok=True)
            (path / ".gitkeep").touch()
        else:
            # Seeded with the purpose, not with content. The loop fills these
            # in as it produces evidence; a file that starts out plausible but
            # empty is worse than one that says it is empty.
            path.write_text(f"# {rel}\n\n{purpose}\n\n_Not yet filled in._\n",
                            encoding="utf-8")
        written.append(rel)
    if args.json:
        print(json.dumps({"root": str(root), "written": written}, indent=2))
    else:
        print(f"logschema: wrote {len(written)} entries to {root}")
        for rel in written:
            print(f"  {rel}")
        print("\nNext: logschema inspect <path-to-log-data>")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    from .inspect_cmd import cmd_inspect

    return cmd_inspect(args)


def _cmd_validate(args: argparse.Namespace) -> int:
    from .validate_cmd import cmd_validate

    return cmd_validate(args)


def _cmd_conformance(args: argparse.Namespace) -> int:
    from .conformance import cmd_conformance

    return cmd_conformance(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logschema",
        description="Build and validate log-ingestion schemas.",
    )
    p.add_argument("--version", action="store_true", help="print version and exit")
    sub = p.add_subparsers(dest="verb")

    h = sub.add_parser("help", help="basic help, or a named topic")
    h.add_argument("topic", nargs="?", choices=["agent-startup", "agent-detail"])
    h.set_defaults(fn=_cmd_help)

    pr = sub.add_parser("project", help="project scaffolding")
    prsub = pr.add_subparsers(dest="subverb", required=True)
    pi = prsub.add_parser("init", help="create a project directory")
    pi.add_argument("--name", required=True, help="project name")
    pi.add_argument("--location", required=True, help="parent directory")
    pi.add_argument("--force", action="store_true", help="write into a non-empty dir")
    pi.add_argument("--json", action="store_true", help="machine-readable output")
    pi.set_defaults(fn=_cmd_project_init)

    insp = sub.add_parser(
        "inspect",
        help="measure a log file and emit questions for the user",
        description=(
            "Measure a log file: field inventory, event clock, identity "
            "candidates with their verdicts, event classes, and the questions "
            "that follow. Produces measurement and questions, never a schema."
        ),
    )
    insp.add_argument("path", help="log file to inspect")
    insp.add_argument("--limit", type=int, default=200_000,
                      help="max records to read (default 200000)")
    insp.add_argument("--json", action="store_true",
                      help="machine-readable output")
    insp.set_defaults(fn=_cmd_inspect)

    val = sub.add_parser(
        "validate",
        help="check a schema against the data; exit 0 only if all predicates pass",
        description=(
            "Recompute every claim the schema makes from the data itself. "
            "A claim is never accepted because it was asserted. Exit code is "
            "the product: 0 validated, 1 not, 2 could not run."
        ),
    )
    val.add_argument("path", help="log file the schema describes")
    val.add_argument("--schema", required=True, help="schema.yaml to check")
    val.add_argument("--limit", type=int, default=200_000,
                     help="max records to read (default 200000)")
    val.add_argument("--time-field", default=None,
                     help="override the clock instead of measuring one")
    val.add_argument("--iteration", type=int, default=None,
                     help="record which loop iteration this was")
    val.add_argument("--json", action="store_true", help="machine-readable output")
    val.set_defaults(fn=_cmd_validate)

    conf = sub.add_parser(
        "conformance",
        help="generate and run the negative fixtures",
        description=(
            "Negative fixtures: deliberately wrong schemas that MUST fail. "
            "Without them a passing validator has not been shown to detect "
            "anything."
        ),
    )
    confsub = conf.add_subparsers(dest="subverb", required=True)
    cw = confsub.add_parser("write", help="generate fixtures for a data file")
    cw.add_argument("path", help="log file the schema describes")
    cw.add_argument("--schema", required=True, help="schema.yaml to mutate")
    cw.add_argument("--out", required=True, help="directory to write fixtures into")
    cw.add_argument("--limit", type=int, default=200_000)
    cw.add_argument("--time-field", default=None)
    cw.set_defaults(fn=_cmd_conformance)
    cr = confsub.add_parser("run", help="run the fixtures; all must fail")
    cr.add_argument("path", help="log file the schema describes")
    cr.add_argument("--fixtures", required=True, help="directory of fixtures")
    cr.add_argument("--limit", type=int, default=200_000)
    cr.add_argument("--time-field", default=None)
    cr.add_argument("--json", action="store_true")
    cr.set_defaults(fn=_cmd_conformance)

    # `doctor` is declared in VERBS as planned and is deliberately NOT
    # registered here. A stub that prints "not yet implemented" and exits 0
    # reads as success to an agent, which is worse than a refusal: argparse's
    # "invalid choice" with a list of what does exist is unambiguous.

    return p


def main(argv: list[str] | None = None) -> int:
    from . import __version__

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        print(f"logschema {__version__}")
        return 0
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):
        print(_brief())
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
