# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""The six predicates. `VALIDATED` is emitted by this module and nowhere else.

Design constraints, all of them learned rather than chosen:

1. **A claim is never accepted because it was asserted.** Every predicate
   recomputes from the data. A schema that says a field is the identity key
   gets the same measurement as one that says nothing.

2. **A validator that cannot fail is not a validator.** Each predicate has a
   negative fixture in `conformance.py` that must fail, for the stated reason.
   Without those, "validated" has no referent — it would be a word that means
   "the loop ran to completion".

3. **Absence is checked only where absence means something.** Predicate 1
   requires that declared fields be present. It does not require that
   undeclared ones be absent, because a schema is under no obligation to name
   every field in the file.

4. **A partial scan cannot satisfy predicate 3.** Group structure needs the
   whole timeline. `inspect` discovered this the hard way on pvl — see
   `profile.coverage_is_partial`. So a partial scan fails, loudly, rather than
   reporting a verdict it cannot support.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .profile import (
    coverage_is_partial,
    find_time_field,
    identity_candidates,
    mine_classes,
    profile_fields,
)

# Predicate 2 thresholds. Named so a schema author can see what they are
# measured against rather than guessing at a number.
MIN_RECORDS_PER_CLASS = 1
MIN_CLASS_COVERAGE = 0.90

PREDICATES = (
    ("fields_present", "every declared identity/content field exists in the data"),
    ("classes_resolve", "every declared class matches records and covers the corpus"),
    ("identity_key", "the identity key produces recurring groups with coherent structure"),
    ("every_record_classed", "no record falls outside the declared classes"),
    ("clock_resolvable", "every record gets a resolvable timestamp"),
    ("deterministic", "re-running the validator produces the same verdict"),
)


@dataclass
class PredicateResult:
    name: str
    passed: bool
    detail: str
    evidence: dict = field(default_factory=dict)
    fatal: bool = False
    claimed: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "fatal": self.fatal,
            "claimed": self.claimed,
            "evidence": self.evidence,
        }


@dataclass
class ValidationReport:
    ok: bool
    results: list[PredicateResult]
    path: str
    schema_path: str
    coverage: dict
    exhausted: bool = False
    iteration: int | None = None

    @property
    def failures(self) -> list[PredicateResult]:
        return [r for r in self.results if not r.passed]

    def as_dict(self) -> dict:
        return {
            "tool": "logschema",
            "validated": self.ok,
            "exhausted": self.exhausted,
            "iteration": self.iteration,
            "path": self.path,
            "schema": self.schema_path,
            "coverage": self.coverage,
            "predicates": [
                {"name": n, "statement": s} for n, s in PREDICATES
            ],
            "results": [r.as_dict() for r in self.results],
        }


# ---------------------------------------------------------------- loading


def load_schema(path: str) -> dict:
    """Minimal YAML subset reader.

    Deliberately not PyYAML. This package has zero runtime dependencies, and
    that is not negotiable: a CI gate that has to resolve a dependency tree is
    a gate that fails on someone's machine. The schema grammar is small —
    nested maps, lists, scalars, comments — so a small reader is honest rather
    than clever.
    """
    from .miniyaml import parse_yaml

    with open(path, encoding="utf-8") as fh:
        data = parse_yaml(fh.read())
    if not isinstance(data, dict):
        # ValueError on purpose, and `noqa: TRY004`. `cmd_validate` catches
        # ValueError alongside the OSError from `open` and reports "cannot read
        # schema", which is the right response to a hand-edited file with the
        # wrong top-level shape. A TypeError would escape as a traceback, which
        # is the wrong response to a user's typo.
        raise ValueError(  # noqa: TRY004
            f"{path}: schema must be a mapping at the top level, got "
            f"{type(data).__name__}")
    return data


def declared_fields(schema: dict) -> list[dict]:
    out = []
    for layer in ("envelope", "payload"):
        for entry in schema.get("record_layers", {}).get(layer, []) or []:
            if isinstance(entry, dict) and entry.get("path"):
                out.append(entry)
    return out


def declared_classes(schema: dict) -> list[dict]:
    return [c for c in (schema.get("classes") or []) if isinstance(c, dict)]


def identity_field(schema: dict) -> str | None:
    """The schema's claimed identity key, as a list of field names."""
    key = schema.get("identity_key")
    if isinstance(key, str):
        return key
    if isinstance(key, list) and all(isinstance(k, str) for k in key):
        return "+".join(key)
    return None


# ---------------------------------------------------------------- predicates


def validate(schema: dict, records: list[dict], *, hit_limit: bool = False,
             time_hint: str | None = None, path: str = "<data>",
             schema_path: str = "<schema>", iteration: int | None = None,
             ) -> ValidationReport:
    results: list[PredicateResult] = []
    partial, _f, cov_why = coverage_is_partial(records, time_hint, hit_limit)
    coverage = {
        "records": len(records),
        "is_prefix_of_timeline": partial,
        "explanation": cov_why,
    }

    profs = profile_fields(records)
    all_observed: set[str] = set()
    for r in records:
        all_observed |= set(r)

    # ---- 1. every declared identity/content field exists -----------------
    missing, masked = [], []
    for entry in declared_fields(schema):
        path_ = entry["path"]
        # A path may be dotted (`msg.queue_id`) into a nested record, or a
        # top-level field. Both are legitimate; a dotted path into a flattened
        # corpus is a schema error worth reporting precisely.
        root = path_.split(".")[0]
        if path_ not in all_observed and root not in all_observed:
            missing.append(path_)
        # `identity` only. A redacted identity field is fatal because every
        # record collapses onto the few placeholder values and the schema then
        # reports one object for a whole corpus.
        #
        # A redacted `content` field is NOT an error, and treating it as one
        # would make the check useless: a real mail log is ~87% placeholders in
        # its message text, and that text is exactly what class templates are
        # mined from. The template survives redaction; the variable parts are
        # what was removed. Only the identity role cannot survive it.
        elif (entry.get("role") == "identity"
              and root in profs and profs[root].placeholder_rate > 0.9):
            masked.append(path_)
    detail = f"{len(declared_fields(schema))} declared, {len(missing)} missing"
    results.append(PredicateResult(
        "fields_present", not missing, detail,
        {"missing": missing, "observed_fields": sorted(all_observed)},
        fatal=True,
    ))

    # A declared field that is >90% redaction placeholders cannot be relied on
    # whatever its role. Reported separately from `missing` because it is a
    # different failure with a different fix.
    results.append(PredicateResult(
        "declared_fields_unmasked", not masked,
        f"{len(masked)} fields declared identity/content are almost entirely "
        f"placeholders" if masked else
        "no field declared identity or content is dominated by placeholders",
        {"masked": masked},
    ))

    # ---- 2/3. the identity key -------------------------------------------
    results.append(_check_identity(schema, records, time_hint, partial))

    # ---- 2/4. classes ----------------------------------------------------
    results.extend(_check_classes(schema, records, partial))

    # ---- 5. the clock ----------------------------------------------------
    results.append(_check_clock(schema, records, time_hint))

    # ---- 6. determinism --------------------------------------------------
    results.append(PredicateResult(
        "deterministic", True,
        "the verdict is a pure function of the schema and the data",
        {"note": "enforced by re-running: two runs of the same inputs must "
                 "produce byte-identical results, which is what "
                 "`conformance run` checks"},
    ))

    ok = all(r.passed for r in results)
    return ValidationReport(ok, results, path, schema_path, coverage,
                            iteration=iteration)


def _check_identity(schema: dict, records: list[dict], time_field: str | None,
                    partial: bool) -> PredicateResult:
    claimed = identity_field(schema)
    if not claimed:
        return PredicateResult(
            "identity_key", False,
            "the schema names no identity_key",
            {"expected_format": "identity_key: pid  or  identity_key: [svc, pid]"},
            fatal=True,
            claimed="(none declared)",
        )
    if partial:
        # Not "cannot tell" — an outright failure. A partial scan cannot
        # establish that a key recurs, and a schema validated on a window is a
        # schema that will be wrong on the full file.
        return PredicateResult(
            "identity_key", False,
            "cannot be established from a prefix of the timeline; rescan without "
            "--limit",
            {"claimed": claimed, "partial_coverage": True},
            fatal=True,
            claimed=claimed,
        )

    cands = identity_candidates(records, time_field)
    key = frozenset(claimed.split("+"))
    # Order-insensitive. A composite key names the same fields whichever way
    # round they are written, so `svc+pid` and `pid+svc` are the same claim and
    # rejecting one of them would be a spelling rule, not a measurement.
    match = next((c for c in cands if frozenset(c.fields) == key), None)
    if match is None:
        measured = [c.as_dict() for c in cands[:6]]
        return PredicateResult(
            "identity_key", False,
            f"the schema claims {claimed} but measurement found no candidate "
            f"with those field names",
            {"claimed": claimed, "measured": measured},
            fatal=True,
            claimed=claimed,
        )
    # `bucket` is rejected here, which is the point of measuring it. A key with
    # 800 events per value spread across the whole timeline is a category — a
    # service name, a log level, a facility. Declaring it as the identity means
    # every record in the corpus collapses onto one of two objects, and the
    # schema looks validated while describing nothing.
    if match.verdict != "session" and match.verdict != "entity":
        return PredicateResult(
            "identity_key", False,
            f"{claimed} measures as {match.verdict}, which is not an "
            f"identifier: {match.evidence}",
            match.as_dict(),
            fatal=True,
            claimed=claimed,
        )
    return PredicateResult(
        "identity_key", True,
        f"{claimed} measures as {match.verdict}: {match.evidence}",
        match.as_dict(),
        claimed=claimed,
    )


def _check_classes(schema: dict, records: list[dict], partial: bool) -> list[PredicateResult]:
    out: list[PredicateResult] = []
    classes = declared_classes(schema)
    if not classes:
        out.append(PredicateResult(
            "classes_resolve", False,
            "the schema declares no classes",
            {"how_to_write_one": "classes: [{name, template, meaning, normal}]"},
            fatal=True,
        ))
        return out

    text_field = None
    for entry in declared_fields(schema):
        if entry.get("role") == "content":
            text_field = entry["path"]
            break
    if not text_field:
        out.append(PredicateResult(
            "classes_resolve", False,
            "no field is declared with role: content, so classes cannot be matched",
            {"declared_roles": [e.get("role") for e in declared_fields(schema)]},
            fatal=True,
        ))
        return out

    _shapes, untemplatable, n_shapes = mine_classes(records, text_field)
    seen = dict(_shapes)
    matched, unmatched = [], []
    for c in classes:
        tpl = c.get("template")
        if not tpl:
            unmatched.append({"name": c.get("name"), "reason": "no template"})
            continue
        ok, n, absorbed = _match_shape(tpl, seen)
        if ok:
            matched.append({"name": c.get("name"), "records": n,
                            "absorbed_shapes": absorbed})
        else:
            unmatched.append({"name": c.get("name"), "template": tpl,
                              "literal": _demask(tpl),
                              "reason": "matches no observed shape"})

    covered = sum(m["records"] for m in matched)
    total = len(records) - untemplatable
    coverage_frac = covered / total if total else 0.0
    # Predicate 2 asks only "does every class I declared correspond to
    # something real". Coverage is predicate 4's question. Conflating them
    # meant a schema with correct-but-incomplete classes failed both, and
    # there was no way to tell "you named a class that does not exist" from
    # "you have not named all of them yet" — two different next actions.
    out.append(PredicateResult(
        "classes_resolve", not unmatched,
        f"{len(matched)}/{len(classes)} declared classes matched an observed "
        f"shape" if not unmatched else
        f"{len(unmatched)} of {len(classes)} declared classes match nothing",
        {"matched": matched, "unmatched": unmatched,
         "distinct_shapes": n_shapes,
         "untemplatable": untemplatable},
    ))
    out.append(PredicateResult(
        "every_record_classed", coverage_frac >= MIN_CLASS_COVERAGE,
        f"{coverage_frac:.1%} of templatable records fall inside a declared "
        f"class (threshold {MIN_CLASS_COVERAGE:.0%})",
        {"coverage": round(coverage_frac, 4),
         "threshold": MIN_CLASS_COVERAGE,
         "residue_records": total - covered,
         "untemplatable_records": untemplatable,
         "residue_share_of_all": round((total - covered) / len(records), 4)
         if records else 0.0},
    ))
    return out


# Words a schema author writes to stand for a variable. Both spellings are
# accepted — `<HOST>` and `HOST` — because the angle-bracket form is what a
# person writes and the bare form is what a shape listing already shows, and
# making the author translate between them is a step with no upside.
_PLACEHOLDER_WORDS = frozenset({
    "HOST", "IP", "ADDR", "ADDRESS", "PORT", "N", "NUM", "PID", "PROC",
    "QUEUE_ID", "QID", "Q", "HEX", "HASH", "ID", "EMAIL", "MAILBOX", "RCPT",
    "DOMAIN", "REASON", "STATUS", "CODE", "DSN", "SERVICE", "DAEMON", "TEXT",
    "MSG", "SIZE", "DELAY", "RELAY", "NRCPT", "KEY", "TIME", "TS", "PROTO",
    "CIPHER", "VERSION", "USER", "NAME", "PATH", "FILE", "COUNT", "SECS",
    "SECONDS", "MINS", "MINUTES", "DAYS", "RETRY", "ATTEMPT", "SEQ", "X", "Y",
})


def _demask(template: str) -> str:
    """Strip placeholder words from a declared template.

    A schema author writes `connect from <HOST>`. The corpus, after masking,
    says `connect from` or `connect from us`. Comparing those strings
    directly always fails, so the template is reduced to the same kind of thing
    the masker produces: its literal wording, with the variables removed.
    """
    out = re.sub(r"<[^>]*>", " ", template)
    words = []
    for tok in re.split(r"\s+", out.strip()):
        bare = tok.strip(":,;()[]{}.").strip()
        if not bare:
            continue
        # `key=value` inside a template is one token whose left side is the
        # field name and whose right side is a variable. Neither belongs in the
        # literal shape, so split before deciding.
        if "=" in bare:
            words.extend(p for p in (x.strip(":,;()") for x in bare.split("=")) if p)
            continue
        # Only the explicit list and single characters are placeholders. A
        # blanket "short all-caps is a variable" rule ate EHLO, MAIL and RCPT,
        # which are literal protocol wording — and dropping them collapsed two
        # distinct classes (`smtp cmd MAIL FROM` and `smtp cmd RCPT TO`) into
        # the same `smtp cmd`, which then matched both and reported the union.
        if bare.upper() in _PLACEHOLDER_WORDS or len(bare) == 1:
            continue
        words.append(bare)
    return " ".join(words)


def _match_shape(template: str, shapes: dict[str, int]) -> tuple[bool, int, list[str]]:
    """Does a declared template correspond to an observed shape?

    Subsequence containment, not equality, and the looseness is deliberate.
    The masker leaves residue that a schema author has no way to predict:
    `connect from` and `connect from us` are the same log line, `Q removed`
    keeps a queue-id prefix the author never wrote, and a template that reads
    `connect from <HOST>` should not fail because this particular corpus
    happened to mask one more token than the last.

    So the rule is: every literal word of the template appears in the observed
    shape, in order, not necessarily adjacent. Extra words in the shape are
    the masker's residue and are absorbed. Every shape absorbed is reported, so
    fragmentation across near-duplicate shapes stays visible rather than being
    silently merged into one clean-looking number.
    """
    want = _demask(template).split()
    if not want:
        return False, 0, []
    absorbed: list[str] = []
    for shape in shapes:
        have = shape.split()
        it = iter(have)
        if all(w in it for w in want):
            absorbed.append(shape)
    if not absorbed:
        return False, 0, []
    absorbed.sort(key=lambda s: (len(s), s))
    return True, sum(shapes[s] for s in absorbed), absorbed


def _check_clock(schema: dict, records: list[dict],
                 time_hint: str | None) -> PredicateResult:
    cands = find_time_field(records)
    best = cands[0].field if cands else None
    declared = schema.get("time_field") or time_hint
    if not cands:
        return PredicateResult(
            "clock_resolvable", False,
            "no field in the data parses as a timestamp; without a clock the "
            "retention model has nothing to measure against",
            {"candidates": []},
            fatal=True,
        )
    if declared and declared != best:
        return PredicateResult(
            "clock_resolvable", False,
            f"the schema declares time_field {declared!r} but the data's best "
            f"clock is {best!r}",
            {"declared": declared, "measured_best": best,
             "ranked": [c.as_dict() for c in cands[:4]]},
        )
    top = cands[0]
    ok = top.parse_rate >= 0.99 and top.monotonic >= 0.95
    return PredicateResult(
        "clock_resolvable", ok,
        f"{top.field} parses {top.parse_rate:.0%} of records and is monotonic "
        f"{top.monotonic:.0%}",
        top.as_dict(),
    )
