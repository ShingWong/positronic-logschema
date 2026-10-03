# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""`logschema inspect` — measure a log file and emit questions, not answers.

The division of labour is the whole design. This command produces
measurements and the questions that follow from them; a language model
produces the candidate schema. Neither does the other's job, because the
model is unreliable exactly where this is reliable — asked which field
identifies a session, a model has been observed to decline to name one while
arguing for the fact that makes one obvious.

Output is plain text by default and `--json` on request. It never blocks on
stdin: a harness that does not wire stdin gets a question and an exit code.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .profile import (
    content_candidates,
    coverage_is_partial,
    find_time_field,
    identity_candidates,
    jsonable,
    mine_classes,
    profile_fields,
)
from .substrate import detect, read

DEFAULT_LIMIT = 200_000


def inspect_path(path: str, limit: int = DEFAULT_LIMIT) -> dict:
    sub = detect(path)
    records = list(read(sub, limit=limit))
    if not records:
        raise ValueError(
            f"{path}: detected as {sub.kind} but yielded no records. If this is "
            f"a JSON array, check the file is not truncated."
        )

    profs = profile_fields(records)
    for fp in profs.values():
        fp.distinct = _exact_distinct(records, fp.name)

    times = find_time_field(records)
    time_field = times[0].field if times else None
    content = content_candidates(profs)
    text_field = content[0].name if content else None

    # Whether this scan is the whole timeline decides whether an identity
    # verdict means anything. Measured before the verdicts are formed, not
    # reported alongside them.
    partial, _coverage_frac, coverage_why = coverage_is_partial(
        records, time_field, sub.hit_limit)
    idents = identity_candidates(records, time_field, partial_coverage=partial)
    classes, untemplatable, n_shapes, _all_shapes = mine_classes(
        records, text_field)

    total = len(records)
    masked_fields = [
        {"field": f.name, "placeholder_rate": round(f.placeholder_rate, 4)}
        for f in profs.values() if f.placeholder_rate > 0.01
    ]
    empty_fields = [
        {"field": f.name, "fill_rate": round(f.fill_rate, 4)}
        for f in profs.values() if f.fill_rate < 0.5
    ]

    return {
        "tool": "logschema",
        "version": __version__,
        "path": str(sub.path),
        "substrate": sub.as_dict(),
        "sampled": {
            "records_read": total,
            "limit": limit,
            "truncated": sub.truncated,
            "available": sub.available,
            "counted_exactly": sub.available is not None,
        },
        "fields": [profs[k].as_dict() for k in sorted(profs)],
        "coverage": {
            "is_prefix_of_timeline": partial,
            "explanation": coverage_why,
            "identity_verdicts_are": ("indicative only" if partial
                                      else "measured on the full corpus"),
        },
        "time_candidates": [t.as_dict() for t in times[:5]],
        "content_candidates": [
            {"field": f.name, "fill_rate": round(f.fill_rate, 4),
             "mean_len": round(f.mean_len, 1), "kind": f.kind}
            for f in content[:5]
        ],
        "identity_candidates": [c.as_dict() for c in idents],
        "classes": {
            "text_field": text_field,
            "distinct": n_shapes,
            # Full frequency list, so `validate`'s advice ("inspect them
            # before declaring them") is actually actionable. The plain-text
            # view shows the head; a caller working the loop needs the whole
            # distribution.
            "all_shapes": [{"shape": sh, "records": c} for sh, c in
                           sorted(_all_shapes.items(), key=lambda kv: -kv[1])],
            "untemplatable_records": untemplatable,
            "untemplatable_share": (
                round(untemplatable / (total - untemplatable + untemplatable), 4)
                if (total - untemplatable + untemplatable) else 0.0
            ),
            "top": [{"shape": s, "records": n} for s, n in classes[:12]],
        },
        "data_health": {
            "masked_fields": masked_fields,
            "sparse_fields": empty_fields,
        },
        "questions": _questions(sub, total, profs, times, content, idents,
                                classes, untemplatable, text_field,
                                masked_fields, limit, partial),
    }


def _exact_distinct(records: list[dict], name: str, cap: int = 200_000) -> int:
    return len({str(r.get(name)) for r in records if r.get(name) not in (None, "")})


# ---------------------------------------------------------------- questions


def _questions(sub, total, profs, times, content, idents, classes,
               untemplatable, text_field, masked, limit, partial=False) -> list[dict]:
    """The four questions, each with its evidence and its rejections.

    Written so the agent can relay them verbatim. The rejected options carry
    their reasons, because a summary that keeps only the recommendation throws
    away the measurement and the next person re-derives it.
    """
    qs: list[dict] = []

    qs.append({
        "id": "data",
        "question": "Where is the log data, and is this the whole of it?",
        "evidence": {
            "path": str(sub.path),
            "format": sub.kind,
            "records_read": total,
            "read_limit": limit,
            "truncated": sub.truncated,
            "records_in_file": sub.available,
        },
        "options": ["this file only", "this file is a sample of a larger set",
                    "there are sibling files to add"],
        "note": ("The scan is bounded. If this was truncated, the counts below "
                 "describe the sample, not the corpus."),
    })

    if times:
        best = times[0]
        qs.append({
            "id": "clock",
            "question": "Which field is the event clock?",
            "evidence": {
                "recommended": best.field,
                "strategy": best.strategy,
                "parse_rate": round(best.parse_rate, 4),
                "monotonic": round(best.monotonic, 4),
                "span_days": best.as_dict()["span_days"],
                "others": [t.as_dict() for t in times[1:4]],
            },
            "options": [t.field for t in times[:4]],
            "note": ("Ranked by how each field behaves as a clock, not by its "
                     "name. A field that parses but jumps around is not one. "
                     "Confirm before continuing: every downstream retention "
                     "claim rests on this."),
        })
    else:
        qs.append({
            "id": "clock",
            "question": "Which field is the event clock?",
            "evidence": {"candidates": []},
            "options": [],
            "note": ("No field parsed as a timestamp. Without a usable clock the "
                     "episodic retention model has nothing to measure against, "
                     "and an absent timestamp will fall back to arrival time. "
                     "Ask the user which field carries the event time, or "
                     "whether the file has one at all."),
        })

    if content:
        best = content[0]
        qs.append({
            "id": "content",
            "question": "Which field carries the readable text?",
            "evidence": {
                "recommended": best.name,
                "fill_rate": round(best.fill_rate, 4),
                "mean_len": round(best.mean_len, 1),
                "alternatives": [
                    {"field": f.name, "fill_rate": round(f.fill_rate, 4),
                     "mean_len": round(f.mean_len, 1)} for f in content[1:4]
                ],
            },
            "options": [f.name for f in content[:4]],
            "note": ("Only the text a consumer actually reads matters. A field "
                     "that is mostly empty will not make a useful content "
                     "field however well it is named."),
        })

    ranked = [c for c in idents if c.verdict != "unknown"][:6]
    if ranked:
        note = ("Measured, not guessed. `bucket` matters most: a domain or "
                "path lifted out of a text field recurs constantly and "
                "denotes nothing worth a record. Pairs are offered because "
                "the real key is often composite — a pid alone collides "
                "across services.")
        if partial:
            note += (" WARNING: this scan is a prefix of the timeline, so "
                     "keys whose repeats fall later read as undetermined. "
                     "Rescan without --limit before choosing a key.")
        qs.append({
            "id": "identity",
            "question": "Which field identifies a persistent thing?",
            "evidence": {
                "measured": [c.as_dict() for c in ranked],
                "partial_coverage": partial,
                "verdict_meaning": {
                    "session": "few events per key, clustered in time — a conversation",
                    "entity": "few events per key, spread over time — a person or host",
                    "bucket": "very many events per key — a category, not a thing",
                    "noise": "keys almost never repeat — not an identifier",
                    "undetermined": ("the scan did not cover enough timeline "
                                     "to tell; not a negative answer"),
                },
            },
            "options": ["+".join(c.fields) for c in ranked],
            "note": note,
        })

    if text_field:
        share = untemplatable / total if total else 0.0
        qs.append({
            "id": "classes",
            "question": "Are these records a template corpus?",
            "evidence": {
                "text_field": text_field,
                "distinct_shapes": len(classes),
                "untemplatable_share": round(share, 4),
                "top_shapes": [{"shape": s, "records": n} for s, n in classes[:6]],
            },
            "options": ["yes, these are templates", "no, this text is prose"],
            "note": (f"{share:.0%} of records could not be reduced to a shape. "
                     f"High share means this text is free-form and a class "
                     f"inventory built from it would just list the distinct "
                     f"values. Decide before writing any disposition."),
        })

    if masked:
        qs.append({
            "id": "redaction",
            "question": "This file is redacted. Which fields are safe as keys?",
            "evidence": {"masked_fields": masked},
            "options": ["use only the unmasked fields", "the redaction is expected; proceed"],
            "note": ("Placeholder values collapse: every redacted record shares "
                     "one identity, so a schema that keys on them reports one "
                     "session for the whole corpus. This is the failure that "
                     "produces a confident wrong answer rather than an error."),
        })

    return qs


# ---------------------------------------------------------------- render


def render(result: dict) -> str:
    L: list[str] = []
    s = result["substrate"]
    sm = result["sampled"]
    L.append(f"logschema inspect — {result['path']}")
    L.append("")
    L.append(f"  format            {s['kind']}"
             + (f" (delimiter {s['delimiter']!r})" if s["delimiter"] else ""))
    # Line-oriented substrates cannot be counted without reading them, so
    # `available` is None there and the disclosure is qualitative: we stopped at
    # the limit, which means there is more. Saying "of 100" would be a lie, and
    # saying nothing would let a sample be read as a census.
    if sm["truncated"] and sm["available"] is not None:
        read_line = f"  of {sm['available']:,} (TRUNCATED)"
    elif sm["truncated"]:
        read_line = f"  (TRUNCATED at limit {sm['limit']:,})"
    else:
        read_line = f"  (limit {sm['limit']:,})"
    L.append(f"  records read      {sm['records_read']:,}{read_line}")
    L.append(f"  fields            {len(result['fields'])}")
    L.append("")

    L.append("  FIELDS")
    L.append(f"    {'field':<20}{'kind':<13}{'fill':>7}{'distinct':>10}"
             f"{'card':>8}{'len':>8}{'masked':>8}")
    for f in result["fields"]:
        L.append(f"    {f['name'][:19]:<20}{f['kind'][:12]:<13}"
                 f"{f['fill_rate']:>7.2f}{f['distinct']:>10,}"
                 f"{f['cardinality']:>8.3f}{f['mean_len']:>8.0f}"
                 + (f"{f['placeholder_rate']:>8.2f}"
                    if f["placeholder_rate"] > 0 else f"{'-':>8}"))
    L.append("")

    if result["time_candidates"]:
        L.append("  EVENT CLOCK  (ranked by behaviour, not by field name)")
        for t in result["time_candidates"]:
            span = f"{t['span_days']:,.1f}d" if t["span_days"] is not None else "-"
            L.append(f"    {t['field'][:19]:<20}{t['strategy']:<16}"
                     f"parse {t['parse_rate']:.2f}  monotone {t['monotonic']:.2f}"
                     f"  span {span:>10}  score {t['score']:.3f}")
        L.append("")

    cov = result["coverage"]
    if cov["is_prefix_of_timeline"]:
        L.append("  COVERAGE")
        L.append(f"    PREFIX OF THE TIMELINE — identity verdicts below are {cov['identity_verdicts_are']}")
        L.append(f"    {cov['explanation']}")
        L.append("")

    if result["identity_candidates"]:
        L.append(f"  IDENTITY CANDIDATES  ({cov['identity_verdicts_are']})")
        L.append(f"    {'key':<26}{'verdict':<9}{'groups':>9}{'mean':>9}"
                 f"{'p95':>7}{'span':>10}{'ev/day':>9}")
        for c in result["identity_candidates"][:8]:
            key = "+".join(c["fields"])[:25]
            span = (f"{c['group_span_days'] * 1440:,.0f}m"
                    if c["group_span_days"] is not None else "-")
            rate = (f"{c['group_rate_per_day']:.1f}"
                    if c["group_rate_per_day"] is not None else "-")
            L.append(f"    {key:<26}{c['verdict']:<9}{c['groups']:>9,}"
                     f"{c['mean_size']:>9,.1f}{c['p95_size']:>7,}"
                     f"{span:>10}{rate:>9}")
        L.append("    (span = median own-span of one key; rate = its events/day)")
        L.append("")

    cl = result["classes"]
    if cl["text_field"]:
        L.append(f"  EVENT CLASSES  (from {cl['text_field']!r})")
        L.append(f"    distinct shapes          {cl['distinct']:,}")
        L.append(f"    untemplatable share      {cl['untemplatable_share']:.1%}")
        for t in cl["top"][:8]:
            L.append(f"      {t['records']:>9,}  {t['shape'][:66]}")
        L.append("")

    dh = result["data_health"]
    if dh["masked_fields"]:
        L.append("  REDACTED FIELDS")
        for m in dh["masked_fields"]:
            L.append(f"    {m['field'][:19]:<20}{m['placeholder_rate']:>8.1%} "
                     f"of values are placeholders")
        L.append("")
    if dh["sparse_fields"]:
        L.append("  SPARSE FIELDS  (<50% filled)")
        for m in dh["sparse_fields"]:
            L.append(f"    {m['field'][:19]:<20}{m['fill_rate']:>8.1%} filled")
        L.append("")

    L.append(f"  {len(result['questions'])} QUESTIONS FOR THE USER")
    L.append("  Relay these verbatim, including the rejected options and why.")
    for q in result["questions"]:
        L.append("")
        L.append(f"    Q[{q['id']}]  {q['question']}")
        for k, v in q["evidence"].items():
            if k in ("measured", "others", "alternatives", "top_shapes"):
                continue
            L.append(f"           {k}: {_short(v)}")
        if q["options"]:
            L.append(f"           options: {', '.join(str(o) for o in q['options'])}")
        L.append(f"           note: {q['note']}")
    L.append("")
    L.append("  Nothing above is a schema. This is measurement and questions;")
    L.append("  the schema is drafted from it, then checked by `logschema validate`.")
    return "\n".join(L)


def _short(v) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, list):
        return f"[{len(v)} items]"
    return str(v)[:90]


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        result = inspect_path(args.path, limit=args.limit)
    except (FileNotFoundError, IsADirectoryError, ValueError) as exc:
        print(f"logschema: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2, default=jsonable))
    else:
        print(render(result))
    return 0
