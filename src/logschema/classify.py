# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Record -> class assignment for validated schemas.

`validate` proves the schema's classes resolve against the data; this is
the consumer side -- turning that proof into a function the ingester calls
per record. Split out (rather than a validate.py helper) because it is a
different job with a different caller: validation runs once per schema,
classification runs once per record, and the ingester (memeng, PAI, anyone)
should import exactly this.

Assumes a VALIDATED schema: every class carries a usable retention and the
templates were matched during validation. An unknown retention here means
the caller skipped validation -- fail loud, never inherit quietly.
"""

from .profile import skeleton
from .validate import _match_shape


def content_field(schema: dict) -> str | None:
    """The record field carrying the matchable text (role: content).

    Mirrors what every consumer needs: without it there is nothing to
    skeletonize and every record is unclassified. A schema with no content
    role fails validation first (fields_present), so None here means the
    caller skipped that step.
    """
    for layer in ("envelope", "payload"):
        for e in (schema.get("record_layers") or {}).get(layer, []) or []:
            if isinstance(e, dict) and e.get("role") == "content":
                return e.get("path")
    return None


def build_classifier(schema: dict, records: list[dict]):
    """Build `classify(record) -> class name | None` plus a coverage report.

    Claims are sorted longest-template-first so the most specific class wins
    a shape two classes could absorb; the first claim on a shape owns it.
    A blank-template class is the empty class for silence (empty/missing
    content classifies to it); a shape no template absorbs is None --
    unclassified, never forced, because a forced class is a lie the arousal
    signal then treats as evidence.

    The report discloses coverage: shapes observed, classes that claimed,
    records classified vs unclassified. An ingester that drops the
    unclassified count is flying blind about what its classes miss.
    """
    field = content_field(schema)
    classes = schema.get("classes") or []
    empty = next((c["name"] for c in classes
                  if not (c.get("template") or "").strip()), None)

    by_shape: dict[str, int] = {}
    for r in records:
        t = r.get(field) if field else None
        if t:
            sh = skeleton(str(t))
            if sh:
                by_shape[sh] = by_shape.get(sh, 0) + 1

    claims = []
    for c in classes:
        tpl = c.get("template") or ""
        if not tpl.strip():
            continue
        ok, _n, absorbed = _match_shape(tpl, by_shape)
        if ok:
            claims.append((c["name"], tpl, absorbed))
    claims.sort(key=lambda c: -len(c[1].split()))
    owner: dict[str, str] = {}
    for name, _tpl, absorbed in claims:
        for sh in absorbed:
            if sh not in owner:
                owner[sh] = name

    def classify(record: dict):
        t = record.get(field) if field else None
        if not t:
            return empty
        sh = skeleton(str(t))
        return owner.get(sh) if sh else None

    classified = sum(1 for r in records if classify(r) is not None)
    report = {
        "content_field": field,
        "shapes_observed": len(by_shape),
        "classes_claimed": len(claims),
        "records_seen": len(records),
        "records_classified": classified,
        "records_unclassified": len(records) - classified,
        "empty_class": empty,
    }
    return classify, report
