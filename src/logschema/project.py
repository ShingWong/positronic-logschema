# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Record projection: a log record plus a schema becomes a normalized event.

This is the seam between `logschema` and whatever consumes the output. It is a
library, not a verb, because the consumer is not ours — positronic is one, a
log analyzer is another, and neither should have to re-implement the masking
rules.

Three rules govern the projection, and all three exist because of a way the
naive version presents wrong data confidently:

1. **A redacted value is never passed through as a value.** It becomes
   `null` with `state: redacted` and the placeholder recorded separately. A
   consumer that stringifies the record must not be able to mistake `<HEX>`
   for an IP address, and a consumer that groups by identity must not collapse
   every redacted record onto one key. This is the single most likely way a
   log pipeline reports something confidently and wrongly.

2. **Provenance travels with every field.** `exact`, `inferred`, or
   `unresolved`, per field. A timestamp that fell back to arrival time is not
   the same fact as one read from the record, and a consumer that cannot tell
   them apart will report a wall-clock time for an event that happened at
   another.

3. **Identity is the schema's claim, checked.** The projection does not
   re-decide the identity key — `validate` already did, and this runs after.
   It records which fields the key came from and what the resulting
   `canonical_name` is, so a consumer can build `(kind, canonical_name)`
   objects and dedupe on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .profile import _STR_FORMATS, PLACEHOLDER, content_candidates, profile_fields
from .validate import (
    _match_shape,
    declared_fields,
    extract_message_ids,
    identity_field,
)


@dataclass
class FieldValue:
    name: str
    value: object = None
    state: str = "exact"        # exact | redacted | absent | empty | unresolved
    placeholder: str | None = None

    def as_dict(self) -> dict:
        d = {"name": self.name, "value": self.value, "state": self.state}
        if self.placeholder is not None:
            d["placeholder"] = self.placeholder
        return d


@dataclass
class Projected:
    index: int
    wall: str | None = None
    wall_precision: str = "unresolved"
    identity_fields: tuple[str, ...] = ()
    canonical_name: str | None = None
    kind: str | None = None
    message_ids: list[str] = field(default_factory=list)
    message_name: str | None = None
    class_name: str | None = None
    class_template: str | None = None
    stream_hint: str | None = None
    content_field: str | None = None
    subject_norm: str | None = None
    body_text: str | None = None
    redaction_share: float = 0.0
    fields: list[FieldValue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "wall": self.wall,
            "wall_precision": self.wall_precision,
            "identity": {
                "fields": list(self.identity_fields),
                "kind": self.kind,
                "canonical_name": self.canonical_name,
            },
            "message": {
                "name": self.message_name,
                "ids": list(self.message_ids),
            },
            "class": {"name": self.class_name, "template": self.class_template},
            "stream_hint": self.stream_hint,
            "content": {
                "field": self.content_field,
                "subject_norm": self.subject_norm,
                "body_text": self.body_text,
                "redaction_share": round(self.redaction_share, 4),
            },
            "fields": [f.as_dict() for f in self.fields],
            "notes": list(self.notes),
        }


class Projector:
    """Holds the schema's decisions; projects records through them.

    Built once per run. Holds the class template index and the identity key, so
    projecting N records does not re-derive either per record.
    """

    def __init__(self, schema: dict, records: list[dict]) -> None:
        self.schema = schema
        self.key = tuple(identity_field(schema).split("+")) \
            if identity_field(schema) else ()
        self.kind = schema.get("identity_kind") or _default_kind(self.key)

        self.time_field = schema.get("time_field")
        self.time_strategy = schema.get("time_strategy", "auto")

        entries = declared_fields(schema)
        self.declared_paths = [e["path"] for e in entries]
        roles = {e["path"]: e.get("role") for e in entries}
        self.content_field = schema.get("content_field") or next(
            (p for p, r in roles.items() if r == "content"), None)

        profs = profile_fields(records)
        cands = content_candidates(profs)
        self.text_field = (self.content_field
                           or (cands[0].name if cands else None))
        self.redaction = {
            k: v.placeholder_rate for k, v in profs.items()
        }

        self.classes = [
            (c["name"], c.get("template", ""), c)
            for c in (schema.get("classes") or [])
            if isinstance(c, dict) and c.get("name")
        ]
        self.class_index = self._build_class_index(records)

    def _build_class_index(self, records: list[dict]) -> dict[str, str]:
        """Map an observed shape to the declared class that covers it."""
        from .profile import mine_classes

        _top, _unt, _n, shapes = mine_classes(records, self.text_field)
        index: dict[str, str] = {}
        for name, tpl, _decl in self.classes:
            ok, _n_rec, absorbed = _match_shape(tpl, dict(shapes))
            if ok:
                for s in absorbed:
                    index.setdefault(s, name)
        return index

    # ------------------------------------------------------------------

    def project(self, record: dict, index: int) -> Projected:
        p = Projected(index=index)
        from .profile import skeleton

        for path in self.declared_paths or list(record):
            root = path.split(".")[0]
            raw = record.get(root)
            fv = _field_value(root, raw)
            p.fields.append(fv)

        # --- the clock, with its precision -------------------------------
        p.wall, p.wall_precision = self._wall(record)
        if p.wall_precision == "unresolved":
            p.notes.append("no resolvable timestamp; consumer must decide a "
                           "fallback rather than inherit one silently")

        # --- identity ----------------------------------------------------
        p.identity_fields = self.key
        if self.key:
            parts = []
            for k in self.key:
                v = record.get(k)
                parts.append("(redacted)" if _is_redacted(v) else str(v))
            p.canonical_name = ":".join(parts)
            p.kind = self.kind
            if any(_is_redacted(record.get(k)) for k in self.key):
                p.notes.append("identity contains a redacted component; the "
                               "canonical name is not resolvable to one entity")

        # --- the message axis ------------------------------------------
        # The second identity, when the schema declares one. A filter
        # re-injection names two queue ids; both are kept, because the join
        # exists to follow the handoff and the first id alone ends at it.
        mk = self.schema.get("message_key") or {}
        if mk.get("pattern") and mk.get("field"):
            p.message_name = mk.get("name") or "message_id"
            try:
                p.message_ids = extract_message_ids(self.schema, record)
            except ValueError as e:
                p.notes.append(str(e))

        # --- class -------------------------------------------------------
        if self.text_field:
            shape = skeleton(str(record.get(self.text_field) or ""))
            if shape is not None and shape in self.class_index:
                p.class_name = self.class_index[shape]
                p.class_template = next(
                    (t for n, t, _ in self.classes if n == p.class_name), None)
            else:
                p.notes.append("no declared class covers this record's shape")

        # --- content, and what a consumer would actually read -------------
        p.content_field = self.text_field
        if self.text_field:
            raw = record.get(self.text_field)
            txt = "" if raw is None else str(raw)
            p.subject_norm = txt
            p.body_text = txt
            share = self.redaction.get(self.text_field, 0.0)
            p.redaction_share = share
            if share > 0.5:
                p.notes.append(f"content is {share:.0%} redaction placeholders; "
                               f"the shape survives, the values do not")

        # --- stream ------------------------------------------------------
        # A hint, not a decision. Which stream a record belongs to is a policy
        # choice about how many clocks exist; the schema can suggest it and the
        # consumer can disagree.
        p.stream_hint = schema_stream(self.schema, record)
        return p

    def _wall(self, record: dict) -> tuple[str | None, str]:
        if self.time_field:
            v = record.get(self.time_field.split(".")[0])
            for _s, parse in _STR_FORMATS:
                d = parse(v) if v is not None else None
                if d is not None:
                    return d.isoformat(), "exact"
            return None, "unresolved"
        for path in self.declared_paths:
            for _s, parse in _STR_FORMATS:
                v = record.get(path.split(".")[0])
                d = parse(v) if v is not None else None
                if d is not None:
                    return d.isoformat(), "inferred"
        return None, "unresolved"


def _field_value(name: str, raw) -> FieldValue:
    if raw is None:
        return FieldValue(name, None, "absent")
    s = str(raw)
    if s == "":
        return FieldValue(name, None, "empty")
    m = PLACEHOLDER.search(s)
    if m:
        return FieldValue(name, None, "redacted", placeholder=m.group(0))
    return FieldValue(name, s, "exact")


def _is_redacted(v) -> bool:
    return v is not None and bool(PLACEHOLDER.search(str(v)))


def _default_kind(key: tuple[str, ...]) -> str | None:
    """A kind derived from the key's own name, so a consumer has something to
    group objects by without inventing one. Deliberately crude — `svc` reads as
    a service, `pid` as a process — because the alternative is a consumer
    inventing six different kinds for the same key."""
    if not key:
        return None
    return "+".join(key)


def schema_stream(schema: dict, record: dict) -> str | None:
    s = schema.get("stream")
    if isinstance(s, str):
        return s
    if isinstance(s, dict):
        f = s.get("field")
        if f and record.get(f) is not None:
            return str(record[f])
    return None


def project(schema: dict, records: list[dict], limit: int = 20) -> list[Projected]:
    proj = Projector(schema, records)
    return [proj.project(r, i) for i, r in enumerate(records[:limit])]
