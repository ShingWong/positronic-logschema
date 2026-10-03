# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""`logschema validate` — the gate. Exit 0 means validated; non-zero means not.

The exit code is the product. This runs in CI, where a report nobody reads
still has to stop a merge, and where "validated" has to mean one specific
thing rather than "the loop finished".

Output contract, as everywhere in this tool: plain text by default, `--json`
on request, never a blocking prompt. A harness that does not wire stdin gets
an answer and an exit code, never a hang.
"""

from __future__ import annotations

import argparse
import json
import sys

from .profile import jsonable
from .substrate import detect, read
from .validate import PREDICATES, load_schema, validate

# Which predicates decide the exit code. All of them do — `ok` is a
# conjunction — but they are listed so the report can lead with the fatal ones,
# since a fatal failure means the rest was never meaningfully measured.
FATAL_ORDER = ("fields_present", "identity_key", "clock_resolvable")


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        schema = load_schema(args.schema)
    except (OSError, ValueError) as exc:
        print(f"logschema: cannot read schema {args.schema}: {exc}",
              file=sys.stderr)
        return 2

    sub = detect(args.path)
    records = list(read(sub, limit=args.limit))
    if not records:
        print(f"logschema: {args.path} yielded no records as {sub.kind}",
              file=sys.stderr)
        return 2

    # An explicit --time-field is honoured so a schema author can be checked
    # against what they claimed rather than against what we guessed. When they
    # claim nothing, the measurement supplies it and predicate 5 still compares.
    time_hint = args.time_field or schema.get("time_field")

    rep = validate(schema, records, hit_limit=sub.hit_limit,
                   time_hint=time_hint, path=str(sub.path),
                   schema_path=args.schema, iteration=args.iteration)

    if args.json:
        print(json.dumps(rep.as_dict(), indent=2, default=jsonable))
    else:
        print(render(rep))
    return 0 if rep.ok else 1


def render(rep) -> str:
    d = rep.as_dict()
    L: list[str] = []
    verdict = "VALIDATED" if rep.ok else "NOT VALIDATED"
    L.append(f"logschema validate — {rep.schema_path}")
    L.append(f"  against {rep.path}")
    L.append("")
    L.append(f"  {verdict}   {len(rep.results) - len(rep.failures)}/"
             f"{len(rep.results)} predicates passed")
    L.append("")
    L.append("  PREDICATES")
    width = max(len(n) for n, _ in PREDICATES)
    for r in rep.results:
        mark = "pass" if r.passed else "FAIL"
        fatal = " (fatal)" if r.fatal and not r.passed else ""
        L.append(f"    [{mark}] {r.name:<{width}}  {r.detail}{fatal}")
    L.append("")

    cov = d["coverage"]
    if cov["is_prefix_of_timeline"]:
        L.append("  COVERAGE")
        L.append(f"    PREFIX OF THE TIMELINE — {cov['explanation']}")
        L.append("")

    if rep.failures:
        L.append("  WHAT FAILED, AND WHAT TO DO ABOUT IT")
        for r in rep.failures:
            L.append("")
            L.append(f"    {r.name}")
            for line in _advice(r):
                L.append(f"      {line}")
        L.append("")

    if not rep.ok:
        L.append("  This schema is not usable. Fix the failures above, or run")
        L.append("  `logschema inspect` again for a fresh measurement to work from.")
        L.append("  Do not add a field to silence a predicate: a class that")
        L.append("  matches nothing and a field that does not exist are the two")
        L.append("  failures a validator exists to catch.")
    return "\n".join(L)


def _advice(r) -> list[str]:
    """Turn a failure into the next action, from the failure itself."""
    out = []
    ev = r.evidence
    if r.name == "fields_present" and ev.get("missing"):
        out.append(f"not in the data: {', '.join(ev['missing'])}")
        out.append("check the spelling, or the record layer it lives in "
                   "(a dotted path into a flat corpus will never resolve)")
    if r.name == "declared_fields_unmasked" and ev.get("masked"):
        out.append(f"redacted, declared as identity: {', '.join(ev['masked'])}")
        out.append("every record would collapse onto the placeholder values")
        out.append("pick an unmasked field as the key instead")
    if r.name == "identity_key":
        # The claimed key is echoed from the schema, not from the evidence: the
        # "not found" branch has no measurement to carry it, and printing "?"
        # there tells the user nothing about which claim was rejected.
        claimed = r.claimed or ev.get("claimed") or "(unnamed)"
        out.append(f"schema claims: {claimed}")
        out.append(f"measured verdict: {ev.get('verdict', 'not found')}")
        out.append("session = a conversation, entity = a long-lived thing, "
                   "bucket = a category, noise = not an identifier")
        if "prefix of the timeline" in r.detail:
            out.append("rescan without --limit; a window cannot show a key recurring")
        if ev.get("measured"):
            out.append("candidates that did measure:")
            for c in ev["measured"][:5]:
                key = "+".join(c["fields"])
                out.append(f"    {key:<22} {c['verdict']:<10} {c['evidence']}")
        elif "not found" in r.detail:
            out.append("run `logschema inspect` for the candidates on this file")
    if r.name == "classes_resolve" and ev.get("unmatched"):
        for u in ev["unmatched"][:6]:
            out.append(f"class {u['name']}: {u['reason']}")
            if u.get("literal"):
                out.append(f"    literal form: {u['literal']!r}")
            out.append("    run `logschema inspect` and copy a shape it reports")
    if r.name == "every_record_classed":
        out.append(f"coverage {ev.get('coverage', 0):.1%}, threshold "
                   f"{ev.get('threshold', 0):.0%}")
        out.append(f"{ev.get('residue_records', 0):,} records fall outside "
                   f"every declared class")
        out.append("either declare the missing classes or accept the residue "
                   "deliberately — do not raise the threshold to hide it")
    if r.name == "clock_resolvable":
        if ev.get("measured_best"):
            out.append(f"declared {ev.get('declared')}, but the data's best "
                       f"clock is {ev['measured_best']}")
        else:
            out.append("no field parses as a timestamp")
            out.append("without a clock, retention has nothing to measure "
                       "against and the episodic model degrades to arrival time")
    return out or [r.detail]
