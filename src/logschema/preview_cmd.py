# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""`logschema preview` — what ingestion would produce, writing nothing.

The last check before a schema touches real data. It answers the question a
schema author cannot answer by reading the schema: *given this file, what comes
out the other end, and is any of it wrong?*

Output is plain text by default and `--json` on request. Never a blocking
prompt. This verb is safe to run against anything, which is why it is the one
to reach for when a schema is still being argued about.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .project import project
from .substrate import detect, read
from .validate import load_schema


def cmd_preview(args: argparse.Namespace) -> int:
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

    rows = project(schema, records, limit=args.n)

    if args.json:
        print(json.dumps({
            "tool": "logschema",
            "data": str(sub.path),
            "schema": args.schema,
            "records_scanned": len(records),
            "records_shown": len(rows),
            "truncated": sub.truncated,
            "projected": [r.as_dict() for r in rows],
            "summary": summarise(rows, len(records)),
        }, indent=2))
        return 0

    print(render(args, sub, len(records), rows))
    return 0


def summarise(rows, scanned: int) -> dict:
    idents = Counter(r.canonical_name for r in rows if r.canonical_name)
    classes = Counter(r.class_name or "(unclassified)" for r in rows)
    redacted = sum(1 for r in rows if r.redaction_share > 0.5)
    unresolved = sum(1 for r in rows if r.wall_precision == "unresolved")
    return {
        "records_shown": len(rows),
        "distinct_identities": len(idents),
        "classes": dict(classes),
        "records_with_redacted_content": redacted,
        "records_without_a_clock": unresolved,
    }


def render(args, sub, scanned: int, rows) -> str:
    L: list[str] = []
    L.append(f"logschema preview — {args.path}")
    L.append(f"  schema         {args.schema}")
    L.append(f"  scanned        {scanned:,} records"
             + ("  (TRUNCATED)" if sub.truncated else ""))
    L.append(f"  showing        {len(rows)}")
    L.append("")

    if not rows:
        L.append("  nothing to show.")
        return "\n".join(L)

    key = "+".join(rows[0].identity_fields) or "(none)"
    L.append(f"  PROJECTION  identity key: {key}")
    L.append(f"    {'#':>4}  {'wall':<26}{'precision':<11}{'canonical_name':<26}"
             f"{'class':<20}notes")
    for r in rows:
        name = (r.canonical_name or "-")[:25]
        wall = (r.wall or "-")[:25]
        cls = (r.class_name or "(unclassified)")[:19]
        note = "; ".join(r.notes)[:60]
        L.append(f"    {r.index:>4}  {wall:<26}{r.wall_precision:<11}{name:<26}"
                 f"{cls:<20}{note}")
    L.append("")

    s = summarise(rows, scanned)
    L.append("  SUMMARY")
    L.append(f"    distinct identities       {s['distinct_identities']:,}")
    for k, v in sorted(s["classes"].items(), key=lambda kv: -kv[1]):
        L.append(f"      {k[:38]:<40}{v:>6}")
    L.append(f"    content mostly redacted   "
             f"{s['records_with_redacted_content']}/{s['records_shown']}")
    L.append(f"    no resolvable clock       {s['records_without_a_clock']}")
    L.append("")

    f0 = rows[0]
    if f0.fields:
        L.append("  FIELDS AS A CONSUMER SEES THEM")
        L.append(f"    {'field':<16}{'value':<44}{'state':<12}placeholder")
        for f in f0.fields[:14]:
            v = f.value if f.value is not None else "null"
            L.append(f"    {f.name[:15]:<16}{str(v)[:43]:<44}{f.state:<12}"
                     f"{f.placeholder or '-'}")
        L.append("")
        L.append("    A redacted value is null with the placeholder recorded")
        L.append("    separately, so no consumer can read <HEX> as an address.")
        L.append("")

    L.append("  Nothing was written. This is the projection only.")
    return "\n".join(L)
