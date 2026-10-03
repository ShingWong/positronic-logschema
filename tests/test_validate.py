"""`validate` must fail on a wrong schema. That is the entire point of it.

Every test here either presents a deliberately broken schema and asserts the
named predicate rejects it, or checks that a good schema passes for the stated
reason. There is no test of the form "validate returns True" without also
being able to say which wrongness it is sensitive to — a validator that cannot
be shown to reject badness has not been shown to do anything.
"""

from __future__ import annotations

import json

import pytest

from logschema.miniyaml import YamlError, parse_yaml
from logschema.validate import (
    ValidationReport,
    declared_classes,
    declared_fields,
    identity_field,
    load_schema,
    validate,
)

# A schema that should pass everything, written the way a schema author would.
GOOD = """
software: postfix
output_style: maillog

identity_key: pid

time_field: stamp

record_layers:
  envelope:
    - {path: stamp, role: context, type: datetime, meaning: "when the line was written"}
    - {path: ident, role: context, type: string, meaning: "which postfix daemon"}
    - {path: pid, role: identity, type: integer, meaning: "process id"}
    - {path: file, role: context, type: string, meaning: "source file"}
  payload:
    - {path: svc, role: identity, type: string, meaning: "subsystem"}
    - {path: tok, role: content, type: string, meaning: "the log line itself"}
    - {path: q, role: context, type: string, meaning: "queue id, redacted"}

classes:
  - name: connect
    template: "connect from <HOST>"
    meaning: "a client opened a connection"
    normal: true
  - name: disconnect
    template: "disconnect from <HOST>"
    meaning: "the connection ended"
    normal: true
  - name: ehlo
    template: "smtp cmd EHLO mx<N>"
    meaning: "the client announced itself"
    normal: true
  - name: client_identified
    template: "client HOST I HEX"
    meaning: "smtpd resolved the peer"
    normal: true
  - name: sasl_failure
    template: "warning HOST I HEX SASL LOGIN authentication failed EMAIL"
    meaning: "authentication did not complete"
    normal: false
  - name: queue_active
    template: "Q HEX from EMAIL size N nrcpt N queue active"
    meaning: "the queue manager took a message"
    normal: true
  - name: queue_removed
    template: "Q HEX removed"
    meaning: "the message left the queue"
    normal: true
  - name: mail_from
    template: "smtp cmd MAIL FROM EMAIL"
    meaning: "the envelope sender was accepted"
    normal: true
  - name: rcpt_to
    template: "smtp cmd RCPT TO EMAIL"
    meaning: "a recipient was accepted"
    normal: true
  - name: resp_to_ehlo
    template: "smtp resp to EHLO"
    meaning: "the server replied to EHLO"
    normal: true
"""


def check(schema_yaml, records, **kw):
    return validate(parse_yaml(schema_yaml), records, **kw)


def by_name(report: ValidationReport, name: str):
    return next(r for r in report.results if r.name == name)


# ---------------------------------------------------------------- the good case


def test_a_good_schema_passes_every_predicate(records):
    rep = check(GOOD, records, time_hint="stamp", schema_path="good.yaml")
    assert rep.ok, [r.as_dict() for r in rep.failures]
    assert by_name(rep, "identity_key").passed
    assert by_name(rep, "clock_resolvable").passed


def test_identity_verdict_is_reported_with_its_measurement(records):
    rep = check(GOOD, records, time_hint="stamp")
    ev = by_name(rep, "identity_key").evidence
    assert ev["verdict"] == "session", ev
    assert ev["mean_size"] > 5


# ---------------------------------------------------------------- predicate 1


def test_p1_rejects_a_field_that_does_not_exist(records):
    bad = GOOD.replace("- {path: tok, role: content,",
                       "- {path: msg, role: content,")
    rep = check(bad, records, time_hint="stamp")
    assert not by_name(rep, "fields_present").passed
    assert "msg" in by_name(rep, "fields_present").evidence["missing"]
    assert not rep.ok


def test_p1_distinguishes_missing_from_unmasked(records):
    """Redaction is judged per role, not per field.

    `q` is 100% placeholders. Declared as `context` it is inert — nothing
    reads it — so it is not an error. Declared as `identity` it is fatal,
    because every record would collapse onto one of three values. The same
    field, the same data, a different role, a different verdict.
    """
    inert = check(GOOD, records, time_hint="stamp")
    assert by_name(inert, "fields_present").passed, \
        by_name(inert, "fields_present").evidence
    assert by_name(inert, "declared_fields_unmasked").passed, \
        by_name(inert, "declared_fields_unmasked").evidence

    load_bearing = GOOD.replace(
        "- {path: q, role: context, type: string, meaning: \"queue id, redacted\"}",
        "- {path: q, role: identity, type: string, meaning: \"queue id\"}")
    rep = check(load_bearing, records, time_hint="stamp")
    unmasked = by_name(rep, "declared_fields_unmasked")
    assert not unmasked.passed
    assert "q" in unmasked.evidence["masked"]


def test_p1_rejects_a_masked_field_declared_as_identity(records):
    bad = GOOD.replace("identity_key: pid", "identity_key: q")
    rep = check(bad, records, time_hint="stamp")
    assert not rep.ok
    assert not by_name(rep, "identity_key").passed


# ---------------------------------------------------------------- predicate 3


def test_p3_rejects_a_key_that_is_a_bucket(records):
    """`svc` has 2 values over 1,600 records spread across the corpus."""
    bad = GOOD.replace("identity_key: pid", "identity_key: svc")
    rep = check(bad, records, time_hint="stamp")
    p3 = by_name(rep, "identity_key")
    assert not p3.passed
    assert "bucket" in p3.detail, p3.detail
    assert p3.fatal


def test_p3_rejects_a_composite_key_on_a_partial_scan(records):
    """The pvl lesson, enforced in the validator and not only in inspect.

    A schema validated on a window is a schema that is wrong on the file.
    """
    rep = check(GOOD, records[:200], time_hint="stamp", hit_limit=True)
    assert not by_name(rep, "identity_key").passed
    assert "prefix of the timeline" in by_name(rep, "identity_key").detail
    assert by_name(rep, "identity_key").fatal


def test_p3_rejects_a_field_name_that_measurement_never_saw(records):
    bad = GOOD.replace("identity_key: pid", "identity_key: nonexistent")
    rep = check(bad, records, time_hint="stamp")
    p3 = by_name(rep, "identity_key")
    assert not p3.passed
    assert "no candidate" in p3.detail


def test_p3_accepts_a_composite_when_it_measures_better(records):
    schema = GOOD.replace("identity_key: pid", "identity_key: [svc, pid]")
    assert identity_field(parse_yaml(schema)) == "svc+pid"
    rep = check(schema, records, time_hint="stamp")
    assert by_name(rep, "identity_key").passed, by_name(rep, "identity_key").detail


# ---------------------------------------------------------------- predicates 2 and 4


def test_p2_rejects_a_class_that_matches_nothing(records):
    bad = GOOD.replace('template: "Q HEX removed"',
                       'template: "totally unrelated wording here"')
    rep = check(bad, records, time_hint="stamp")
    p2 = by_name(rep, "classes_resolve")
    assert not p2.passed
    names = [u["name"] for u in p2.evidence["unmatched"]]
    assert "queue_removed" in names


def test_p2_rejects_low_coverage(records):
    bad = GOOD.replace('template: "connect from <HOST>"',
                       'template: "a shape that never occurs in this corpus"')
    rep = check(bad, records, time_hint="stamp")
    assert not by_name(rep, "classes_resolve").passed


def test_p4_is_separate_from_p2(records):
    """Two classes can all match and still leave most records unclassified."""
    schema = """
software: x
identity_key: pid
time_field: stamp
record_layers:
  payload:
    - {path: tok, role: content, type: string, meaning: m}
classes:
  - {name: one, template: "connect from HOST", meaning: m, normal: true}
"""
    rep = check(schema, records, time_hint="stamp")
    assert by_name(rep, "classes_resolve").passed, \
        [r.as_dict() for r in rep.failures]
    assert not by_name(rep, "every_record_classed").passed
    assert by_name(rep, "every_record_classed").evidence["residue_records"] > 0


# ---------------------------------------------------------------- predicate 5


def test_p5_rejects_a_declared_clock_that_is_not_the_best_clock(records):
    bad = GOOD.replace("time_field: stamp", "time_field: pid")
    rep = check(bad, records, time_hint="stamp")
    p5 = by_name(rep, "clock_resolvable")
    assert not p5.passed
    assert "pid" in p5.detail and "stamp" in p5.detail


def test_p5_fails_when_the_schema_declares_nothing_and_data_has_no_clock(records):
    stripped = [{"tok": r["tok"], "pid": r["pid"]} for r in records]
    schema = """
software: x
identity_key: pid
record_layers:
  payload:
    - {path: tok, role: content, type: string, meaning: m}
"""
    rep = check(schema, stripped, time_hint=None)
    p5 = by_name(rep, "clock_resolvable")
    assert not p5.passed and p5.fatal
    assert "no field in the data parses as a timestamp" in p5.detail


# ---------------------------------------------------------------- hygiene


def test_a_schema_with_no_identity_key_fails_with_a_usable_message(records):
    schema = GOOD.replace("identity_key: pid\n", "")
    rep = check(schema, records, time_hint="stamp")
    p3 = by_name(rep, "identity_key")
    assert not p3.passed
    assert "identity_key" in p3.evidence["expected_format"]


def test_a_schema_with_no_content_field_fails_classes_with_a_reason(records):
    schema = GOOD.replace("role: content", "role: context")
    rep = check(schema, records, time_hint="stamp")
    p2 = by_name(rep, "classes_resolve")
    assert not p2.passed
    assert "role: content" in p2.detail


def test_report_serialises_and_names_every_predicate(records):
    rep = check(GOOD, records, time_hint="stamp")
    d = rep.as_dict()
    assert d["validated"] is True
    assert len(d["predicates"]) == 6
    assert {r["name"] for r in d["results"]} >= {
        "fields_present", "classes_resolve", "identity_key",
        "clock_resolvable", "deterministic"}
    json.dumps(d)  # must be serialisable for `--json`


def test_validation_is_deterministic(records):
    """Same inputs, byte-identical output. Predicate 6 is a promise the
    implementation has to keep, not a line in the report."""
    a = check(GOOD, records, time_hint="stamp").as_dict()
    b = check(GOOD, records, time_hint="stamp").as_dict()
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------- miniyaml


def test_yaml_subset_parses_the_schema_shape():
    d = parse_yaml(GOOD)
    assert d["software"] == "postfix"
    assert d["identity_key"] == "pid"
    assert len(declared_fields(d)) == 7
    assert len(declared_classes(d)) == 10
    assert [f["path"] for f in declared_fields(d)] == [
        "stamp", "ident", "pid", "file", "svc", "tok", "q"]


def test_yaml_subset_parses_block_sequences_of_mappings():
    d = parse_yaml("""
a:
  - {x: 1, y: two}
  - {x: 3, y: four}
b:
  - plain
  - "quoted: with colon"
""")
    assert d["a"] == [{"x": 1, "y": "two"}, {"x": 3, "y": "four"}]
    assert d["b"] == ["plain", "quoted: with colon"]


def test_yaml_subset_rejects_block_scalars_with_a_line_number():
    with pytest.raises(YamlError, match="line 2"):
        parse_yaml("software: postfix\nmeaning: |\n  some text\n")


def test_yaml_subset_rejects_multi_document_streams():
    with pytest.raises(YamlError, match="multi-document"):
        parse_yaml("a: 1\n---\nb: 2\n")


def test_yaml_subset_rejects_tab_indentation():
    with pytest.raises(YamlError, match="tab"):
        parse_yaml("a:\n\tb: 1\n")


def test_yaml_subset_rejects_a_line_that_is_not_a_mapping():
    with pytest.raises(YamlError, match="expected `key: value`"):
        parse_yaml("just a bare line\n")


def test_load_schema_reads_a_file(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(GOOD, encoding="utf-8")
    assert load_schema(str(p))["software"] == "postfix"
