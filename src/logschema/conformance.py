# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Negative fixtures: schemas that are wrong on purpose and must be rejected.

This is the half of validation that proves the other half. A validator that
passes everything has demonstrated nothing; the word "validated" only has a
referent once a deliberately broken schema has been shown to fail for the
stated reason.

Each fixture is a mutation of a real schema plus the predicate it targets and
the text its failure is required to contain. The required substring matters as
much as the failure: a schema rejected for the wrong reason is not a passing
fixture either, and that is the failure mode a fixture list written by hand
quietly develops.

`write` produces the directory. `run` executes every fixture and reports any
that passed — which is the finding, because it means the validator has a hole
exactly where that fixture probes.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .substrate import detect, read
from .validate import PREDICATES, Measurement, load_schema, validate

FIXTURE_INDEX = "fixtures.json"

# Measurement facts the mutations need, populated by `write_fixtures` from the
# data. Module-level because the mutation callables are plain functions with no
# channel to pass context through, and threading a context object through six
# lambdas to reach three values would be worse. Written once per `write`, read
# immediately, never persisted.
_REDACTED: dict = {"fields": [], "buckets": [], "content_fields": []}
_content_paths: list[str] = []


def _measure_context(records: list[dict], time_field: str | None,
                     content_fields: list[str]) -> None:
    from .profile import identity_candidates, profile_fields

    profs = profile_fields(records)
    # Root names only. Declared paths may be dotted (`msg.queue_id`) while the
    # measurement is per top-level field, so comparing them directly meant a
    # dotted content path never matched its own field and the content exclusion
    # silently did nothing — which is how the content field ended up chosen as
    # the redacted identity key.
    content_roots = {p.split(".")[0] for p in content_fields}
    _REDACTED["content_fields"] = sorted(content_roots)
    _REDACTED["fields"] = [
        k for k, fp in profs.items()
        if fp.non_null and fp.placeholder_rate > 0.9
        and k not in content_roots
    ]
    _REDACTED["buckets"] = [
        "+".join(c.fields) for c in identity_candidates(records, time_field)
        if c.verdict == "bucket"
    ]


# Each mutation: (suffix, target predicate, required substring, mutate, why)
def _mutations() -> list[tuple[str, str, str, object, str]]:
    def drop_field(s: dict) -> dict:
        s = _copy(s)
        for layer in ("envelope", "payload"):
            for e in s.get("record_layers", {}).get(layer, []) or []:
                if isinstance(e, dict) and e.get("role") == "content":
                    e["path"] = "field_that_does_not_exist"
                    return s
        return s

    def bucket_key(s: dict) -> dict:
        # The most valuable fixture in the set. A schema whose identity key is a
        # category passes every field-presence check, matches classes fine, and
        # looks entirely reasonable. It is wrong, and nothing but measurement
        # catches it.
        s = _copy(s)
        for layer in ("envelope", "payload"):
            for e in s.get("record_layers", {}).get(layer, []) or []:
                if isinstance(e, dict) and e.get("role") == "identity":
                    s["identity_key"] = e["path"]
                    return s
        s["identity_key"] = "nonexistent_field"
        return s

    def phantom_class(s: dict) -> dict:
        s = _copy(s)
        s.setdefault("classes", []).append({
            "name": "phantom",
            "template": "wording that appears in no log line anywhere",
            "meaning": "a class invented to prove the validator rejects it",
            "normal": True,
        })
        return s

    def wrong_clock(s: dict) -> dict:
        s = _copy(s)
        s["time_field"] = "field_that_is_not_a_clock"
        return s

    def masked_identity(s: dict) -> dict:
        # Must name a field that IS redacted, and must also be a field whose
        # redaction is the problem rather than its saving grace.
        #
        # The content field is excluded on purpose. A redacted content field is
        # fine — a real mail log is ~87% placeholders in its message text, and
        # that text is what class templates are mined from. The template
        # survives redaction; the variable parts are what was removed. Naming
        # the content field here produced a fixture the validator correctly
        # accepted, and the harness correctly reported a hole.
        s = _copy(s)
        redacted = list(_REDACTED.get("fields") or [])
        if not redacted:
            # Nothing redacted and non-content in this corpus, so the wrongness
            # is genuinely not expressible here. Say so plainly and point the
            # failure at the real gate — `fields_present` will reject a key
            # naming a field that is not in the data, which is the honest
            # nearest thing this corpus can be asked to demonstrate.
            #
            # The alternative was naming a field that does not exist, which
            # silently duplicated `field-does-not-exist` and left this fixture
            # reporting success for a predicate it never exercised.
            s["identity_key"] = "__no_redacted_non_content_field_in_corpus__"
            # `identity_key`, not `fields_present`: the sentinel is a bad
            # *key*, and only the identity predicate rejects a key naming
            # fields that do not exist. `fields_present` checks declared
            # record_layers, which this mutation leaves untouched.
            s["__fixture_note__"] = {
                "target": "identity_key",
                "must_contain": "no candidate",
            }
            return s
        target = redacted[0]
        s["identity_key"] = target
        declared = False
        for layer in ("envelope", "payload"):
            for e in s.get("record_layers", {}).get(layer, []) or []:
                if isinstance(e, dict) and e.get("path") == target:
                    e["role"] = "identity"
                    declared = True
        if not declared:
            # The field must be DECLARED as well as claimed. Naming an
            # undeclared field is caught by `fields_present`, so setting only
            # `identity_key` made this fixture fail (or pass) for the wrong
            # predicate — it was silently duplicating another fixture while
            # `declared_fields_unmasked` had nothing to look at.
            s.setdefault("record_layers", {}).setdefault("payload", []).append({
                "path": target,
                "role": "identity",
                "type": "string",
                "meaning": "a redacted field, declared and claimed as the key",
            })
        return s

    def bucket_key_on_low_cardinality(s: dict) -> dict:
        # Must name a field that actually measures as a bucket. Same reasoning
        # as above: a fixture aimed at the bucket predicate that lands on a
        # session is not testing the bucket predicate.
        s = _copy(s)
        buckets = set(_REDACTED.get("buckets") or [])
        for layer in ("envelope", "payload"):
            for e in s.get("record_layers", {}).get(layer, []) or []:
                if isinstance(e, dict) and e.get("path") in buckets:
                    s["identity_key"] = e["path"]
                    return s
        s["identity_key"] = "file"
        return s

    def no_identity(s: dict) -> dict:
        s = _copy(s)
        s.pop("identity_key", None)
        return s

    return [
        ("field-does-not-exist", "fields_present", "missing", drop_field,
         "a declared field that is not in the data"),
        ("identity-key-is-a-bucket", "identity_key", "not an identifier",
         bucket_key_on_low_cardinality,
         (
             "the identity key measures as a category rather than a thing — "
             "the failure that a field-presence check cannot see"
         )),
        ("class-matches-nothing", "classes_resolve", "match nothing",
         phantom_class,
         "a declared class corresponding to no observed shape"),
        ("clock-is-not-a-clock", "clock_resolvable", "best clock",
         wrong_clock,
         "a declared clock that is not the data's best clock"),
        ("identity-key-is-masked", "declared_fields_unmasked", "placeholders",
         masked_identity,
         (
             "a redacted field declared as the identity key, so every record "
             "collapses onto the placeholders"
         )),
        ("no-identity-key", "identity_key", "identity_key", no_identity,
         "a schema that names no identity key at all"),
    ]


def _copy(d: dict) -> dict:
    import copy

    return copy.deepcopy(d)


def _dump(schema: dict, meta: dict, path: Path) -> None:
    """Write a fixture as YAML-ish text plus its metadata.

    Emitted as YAML rather than JSON so a human can read what the mutation was,
    which is the point of a fixture: the next person has to believe it.
    """
    from .schema_io import dump_yaml

    path.write_text(
        f"# FIXTURE: {meta['id']}\n"
        f"# MUST FAIL: {meta['target_predicate']}\n"
        f"# MUST SAY:   {meta['must_contain']}\n"
        f"# WHY:        {meta['why']}\n"
        f"#\n"
        f"# A fixture that PASSES is a hole in the validator, not a passing test.\n"
        + dump_yaml(schema),
        encoding="utf-8",
    )


def write_fixtures(data_path: str, schema_path: str, out_dir: str,
                   limit: int = 200_000, time_field: str | None = None) -> dict:
    from .profile import find_time_field

    schema = load_schema(schema_path)
    sub = detect(data_path)
    records = list(read(sub, limit=limit))
    if not records:
        raise ValueError(f"{data_path} yielded no records as {sub.kind}")
    hint = time_field or schema.get("time_field")
    if not hint:
        cands = find_time_field(records)
        hint = cands[0].field if cands else None
    from .validate import declared_fields

    _measure_context(records, hint,
                     [e["path"] for e in declared_fields(schema)
                      if e.get("role") == "content"])

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for fid, target, must, mutate, why in _mutations():
        broken = mutate(schema)
        # A mutation that could not be expressed against this corpus retargets
        # itself, rather than pretending. Recorded in the fixture so the run
        # report can say which predicate it actually exercised.
        note = broken.pop("__fixture_note__", None)
        if note:
            target, must = note["target"], note["must_contain"]
        meta = {"id": fid, "target_predicate": target, "must_contain": must,
                "why": why, "source_schema": schema_path, "data": data_path}
        p = out / f"{fid}.yaml"
        _dump(broken, meta, p)
        written.append({"file": p.name, **meta})
    (out / FIXTURE_INDEX).write_text(
        json.dumps({"predicates": [n for n, _ in PREDICATES], "fixtures": written},
                   indent=2),
        encoding="utf-8",
    )
    return {"dir": str(out), "fixtures": written}


def run_fixtures(data_path: str, fixtures_dir: str, limit: int = 200_000,
                 time_field: str | None = None) -> dict:
    fdir = Path(fixtures_dir)
    index_path = fdir / FIXTURE_INDEX
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path} not found. Run `logschema conformance write` first.")
    index = json.loads(index_path.read_text(encoding="utf-8"))

    from .profile import find_time_field

    sub = detect(data_path)
    records = list(read(sub, limit=limit))
    hit_limit = sub.hit_limit
    if not time_field:
        # Fall back to the fixtures' own declared clock, then to a measured
        # one. Running the bucket fixture with no clock turns a session into
        # an entity and the fixture stops testing what it claims to.
        for fx in index["fixtures"]:
            try:
                cand = load_schema(str(fdir / fx["file"])).get("time_field")
            except (OSError, ValueError):
                cand = None
            if cand:
                time_field = cand
                break
    if not time_field:
        cands = find_time_field(records)
        time_field = cands[0].field if cands else None

    # One measurement for the whole run. The facts belong to the data, not to
    # the fixture, and re-deriving them per fixture is what made this command
    # time out on a 1.5M-line file.
    measurement = Measurement(records)

    results = []
    holes = []
    for fx in index["fixtures"]:
        fpath = fdir / fx["file"]
        try:
            schema = load_schema(str(fpath))
        except (OSError, ValueError) as exc:
            results.append({"id": fx["id"], "outcome": "unreadable",
                            "detail": str(exc)})
            holes.append(fx["id"])
            continue
        rep = validate(schema, records, hit_limit=hit_limit,
                       time_hint=time_field, path=str(sub.path),
                       schema_path=fx["file"], cache=measurement)
        target = fx["target_predicate"]
        result = next((r for r in rep.results if r.name == target), None)
        failed = result is not None and not result.passed
        said = result is not None and fx["must_contain"].lower() in \
            (result.detail + " " + json.dumps(result.evidence)).lower()
        if failed and said:
            outcome = "rejected-as-required"
        elif not failed:
            outcome = "PASSED-BUT-SHOULD-FAIL"
            holes.append(fx["id"])
        else:
            outcome = "failed-for-the-wrong-reason"
            holes.append(fx["id"])
        results.append({
            "id": fx["id"],
            "outcome": outcome,
            "target_predicate": target,
            "detail": result.detail if result else "predicate not run",
            "why_it_matters": fx["why"],
        })

    return {
        "data": str(sub.path),
        "fixtures": len(index["fixtures"]),
        "results": results,
        "holes": holes,
        "ok": not holes,
    }


def cmd_conformance(args: argparse.Namespace) -> int:
    try:
        if args.subverb == "write":
            res = write_fixtures(args.path, args.schema, args.out,
                                 limit=args.limit, time_field=args.time_field)
            print(f"logschema: wrote {len(res['fixtures'])} negative fixtures "
                  f"to {res['dir']}")
            for f in res["fixtures"]:
                print(f"  {f['id']:<28} must fail {f['target_predicate']}")
            print("\nEvery one of these is wrong on purpose. Run "
                  "`logschema conformance run` to prove the validator notices.")
            return 0

        res = run_fixtures(args.path, args.fixtures, limit=args.limit,
                           time_field=args.time_field)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(render_run(res))
        return 0 if res["ok"] else 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"logschema: {exc}", file=sys.stderr)
        return 2


def render_run(res: dict) -> str:
    L = ["logschema conformance run", f"  data     {res['data']}", ""]
    for r in res["results"]:
        mark = {"rejected-as-required": "ok  ",
                "PASSED-BUT-SHOULD-FAIL": "HOLE",
                "failed-for-the-wrong-reason": "WRONG",
                "unreadable": "ERR "}[r["outcome"]]
        L.append(f"  [{mark}] {r['id']:<28} {r['target_predicate']}")
        if r["outcome"] != "rejected-as-required":
            L.append(f"           {r['detail'][:96]}")
            L.append(f"           {r['why_it_matters']}")
    n_ok = sum(1 for r in res["results"] if r["outcome"] == "rejected-as-required")
    L.append("")
    L.append(f"  {n_ok}/{len(res['results'])} negative fixtures rejected for the "
             f"stated reason")
    if res["holes"]:
        L.append("")
        L.append("  A fixture that passes is a hole in the validator. A fixture")
        L.append("  that fails for the wrong reason is just as bad: it means the")
        L.append("  predicate is not sensitive to the thing it claims to check.")
    else:
        L.append("  The validator is demonstrably sensitive to every wrongness")
        L.append("  these fixtures encode. That is what makes a pass meaningful.")
    return "\n".join(L)
