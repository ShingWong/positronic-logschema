"""`preview` exists to catch one thing: a projection that reports wrong data
confidently. Everything here is about that.

The specific failure is a redacted value passed through as if it were real.
`<HEX>` is not an address, and a consumer that groups by it collapses every
redacted record onto one identity. That is the failure mode where nothing
errors and the answer is simply false, so it gets the most tests.
"""

from __future__ import annotations

import json

import pytest

from logschema.miniyaml import parse_yaml
from logschema.preview_cmd import summarise
from logschema.project import project

GOOD = """
software: postfix
identity_key: pid
time_field: stamp
record_layers:
  envelope:
    - {path: stamp, role: context, type: datetime, meaning: when}
    - {path: pid, role: identity, type: integer, meaning: process}
    - {path: ident, role: context, type: string, meaning: daemon}
    - {path: file, role: context, type: string, meaning: source}
  payload:
    - {path: svc, role: identity, type: string, meaning: subsystem}
    - {path: tok, role: content, type: string, meaning: the line}
classes:
  - {name: connect, template: "connect from <HOST>", meaning: m, normal: true}
  - {name: sasl, template: "warning SASL LOGIN authentication failed", meaning: m, normal: false}
  - {name: queued, template: "Q <QID> from <EMAIL> size <N> nrcpt <N> queue active", meaning: m, normal: true}
  - {name: removed, template: "Q <QID> removed", meaning: m, normal: true}
  - {name: rcpt, template: "smtp cmd RCPT TO <EMAIL>", meaning: m, normal: true}
  - {name: mail, template: "smtp cmd MAIL FROM <EMAIL>", meaning: m, normal: true}
"""


@pytest.fixture
def schema():
    return parse_yaml(GOOD)


# ---------------------------------------------------------------- redaction


def test_a_redacted_field_is_null_not_the_placeholder_text(schema, records):
    """The failure this whole verb exists for.

    `<HEX>` is not a value. Passing it through means a consumer stringifies it
    and reports an address that does not exist.
    """
    rows = project(schema, records, limit=5)
    for r in rows:
        for f in r.fields:
            if f.state == "redacted":
                assert f.value is None, (
                    f"{f.name} leaked its placeholder as a value: {f.value!r}")
                assert f.placeholder, "redacted field with no placeholder recorded"


def test_content_is_passed_through_verbatim_and_fields_are_not(schema, records):
    """The two halves of redaction behave differently, on purpose.

    `subject_norm` / `body_text` are the raw text, because the surviving shape
    is exactly what classes are mined from and a consumer may want to search
    it. Redaction is still visible in the text — as the placeholder it is.

    Structured `fields` are the opposite: a redacted value is `null` with the
    placeholder recorded separately, because a consumer that groups by a field
    value must not be able to read `<HEX>` as an address.
    """
    rows = project(schema, records, limit=40)
    for r in rows:
        d = r.as_dict()
        for f in d["fields"]:
            assert not (isinstance(f["value"], str) and "<HEX>" in f["value"]), (
                f"{f['name']} leaked a placeholder as a value")
        raw = d["content"]["subject_norm"]
        if isinstance(raw, str) and raw:
            assert any(t in raw for t in ("<HEX>", "<EMAIL>", "<HOST>", "<KEY>")), (
                f"redaction markers vanished from the content text: {raw!r}")


def test_identity_with_a_redacted_component_is_flagged():
    """Grouping by a redacted component collapses everything onto one key."""
    schema = parse_yaml(GOOD.replace("identity_key: pid", "identity_key: q"))
    recs = [{"stamp": "2023-11-14T22:13:22", "pid": 1, "q": "<HEX0>",
             "tok": "Q <HEX0>: removed"} for _ in range(5)]
    rows = project(schema, recs, limit=5)
    assert any("redacted component" in n for n in rows[0].notes), rows[0].notes
    assert rows[0].canonical_name == "(redacted)"


def test_redacted_content_is_reported_but_not_treated_as_an_error(records):
    """98% placeholders in the message text is normal for a mail log.

    The shape survives redaction; the values are what was removed. `preview`
    says so without calling the schema broken.
    """
    rows = project(parse_yaml(GOOD), records, limit=5)
    assert rows[0].redaction_share > 0.5
    assert any("shape survives" in n for n in rows[0].notes)
    assert rows[0].class_name, "redaction destroyed the class assignment"


# ---------------------------------------------------------------- provenance


def test_clock_precision_is_carried_per_record(schema, records):
    rows = project(schema, records, limit=3)
    assert all(r.wall_precision == "exact" for r in rows)
    assert all(r.wall for r in rows)


def test_a_record_with_no_usable_clock_is_unresolved_and_says_so():
    schema = parse_yaml(GOOD.replace("time_field: stamp", "time_field: absent"))
    recs = [{"pid": 1, "tok": "Q <HEX0>: removed"}]
    rows = project(schema, recs, limit=1)
    assert rows[0].wall is None
    assert rows[0].wall_precision == "unresolved"
    assert any("fallback" in n for n in rows[0].notes)


def test_absent_and_empty_fields_are_distinguished_from_redacted():
    schema = parse_yaml(GOOD)
    recs = [{"stamp": "2023-11-14T22:13:22", "pid": 1, "svc": "",
             "tok": "Q <HEX0>: removed", "ident": None}]
    rows = project(schema, recs, limit=1)
    states = {f.name: f.state for f in rows[0].fields}
    assert states["svc"] == "empty"
    assert states["ident"] == "absent"


# ---------------------------------------------------------------- identity


def test_identity_comes_from_the_schema_not_from_a_re_guess(records):
    """The projection must not re-decide the key. `validate` already did."""
    schema = parse_yaml(GOOD.replace("identity_key: pid", "identity_key: svc"))
    rows = project(schema, records, limit=5)
    assert rows[0].identity_fields == ("svc",)
    assert rows[0].canonical_name in ("smtpd", "qmgr")


def test_composite_key_is_joined_in_declared_order():
    schema = parse_yaml(GOOD.replace("identity_key: pid",
                                     "identity_key: [svc, pid]"))
    rows = project(schema, [{"stamp": "2023-11-14T22:13:22", "svc": "smtpd",
                             "pid": 7, "tok": "connect from <HOST>"}], limit=1)
    assert rows[0].canonical_name == "smtpd:7"
    assert rows[0].kind == "svc+pid"


def test_distinct_identities_are_actually_distinct(schema, records):
    """The summary is the thing a user reads first.

    The fixture is 40 sessions of 40 lines, so the first 200 records cover 5
    sessions. Asserting 30 was my arithmetic error, not a projection bug — and
    the failure was worth having, because "one identity for 200 records" is
    exactly the collapse this is checking for.
    """
    rows = project(schema, records, limit=200)
    s = summarise(rows, 200)
    assert s["distinct_identities"] == 5, s
    # And over the whole corpus it must reach the session count, not fewer.
    whole = summarise(project(schema, records, limit=len(records)), len(records))
    assert whole["distinct_identities"] == 40, whole


# ---------------------------------------------------------------- classes


def test_every_shown_record_gets_a_class_or_says_why_not(schema, records):
    rows = project(schema, records, limit=60)
    for r in rows:
        if r.class_name is None:
            assert any("no declared class" in n for n in r.notes)


# ---------------------------------------------------------------- cli


def test_cli_writes_nothing_and_exits_zero(records, jsonl, schema_file, capsys):
    from logschema.cli import main

    assert main(["preview", jsonl, "--schema", schema_file, "-n", "5"]) == 0
    out = capsys.readouterr().out
    assert "Nothing was written" in out
    assert "canonical_name" in out


def test_cli_json_is_serialisable(records, jsonl, schema_file, capsys):
    from logschema.cli import main

    assert main(["preview", jsonl, "--schema", schema_file, "-n", "5",
                 "--json"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["records_shown"] == 5
    assert d["summary"]["distinct_identities"] >= 1
    assert d["projected"][0]["identity"]["kind"]


def test_cli_reports_a_missing_schema_without_a_traceback(tmp_path, capsys):
    from logschema.cli import main

    assert main(["preview", str(tmp_path / "nope"), "--schema",
                 str(tmp_path / "no.yaml")]) == 2
    assert "logschema:" in capsys.readouterr().err


def test_cli_help_lists_preview_as_ready(capsys):
    from logschema.cli import main

    main(["help"])
    out = capsys.readouterr().out
    ready, _, _planned = out.partition("not yet implemented:")
    assert "logschema preview" in ready
