"""The negative fixtures are the point. These tests check they still bite.

If a fixture passes, the validator has a hole. If it fails for a reason other
than the one it encodes, the predicate is not sensitive to the thing it claims
to check. Both are failures, and both are asserted here.
"""

from __future__ import annotations

import json

import pytest

from logschema.conformance import (
    FIXTURE_INDEX,
    run_fixtures,
    write_fixtures,
)
from logschema.schema_io import dump_yaml
from logschema.validate import load_schema, validate

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
  - {name: disconnect, template: "disconnect from <HOST>", meaning: m, normal: true}
  - {name: ehlo, template: "smtp cmd EHLO mx<N>", meaning: m, normal: true}
  - {name: client, template: "client <HOST>", meaning: m, normal: true}
  - {name: sasl, template: "warning SASL LOGIN authentication failed", meaning: m, normal: false}
  - {name: mail_from, template: "smtp cmd MAIL FROM <EMAIL>", meaning: m, normal: true}
  - {name: rcpt_to, template: "smtp cmd RCPT TO <EMAIL>", meaning: m, normal: true}
  - {name: resp, template: "smtp resp to EHLO", meaning: m, normal: true}
  - {name: queued, template: "Q <QID> from <EMAIL> size <N> nrcpt <N> queue active", meaning: m, normal: true}
  - {name: removed, template: "Q <QID> removed", meaning: m, normal: true}
"""


@pytest.fixture
def schema_file(tmp_path):
    p = tmp_path / "schema.yaml"
    p.write_text(GOOD, encoding="utf-8")
    return str(p)


def test_good_schema_passes_before_any_fixture_matters(records, schema_file):
    rep = validate(load_schema(schema_file), records, time_hint="stamp")
    assert rep.ok, [r.as_dict() for r in rep.failures]


def test_write_then_run_rejects_every_fixture(records, jsonl, schema_file, tmp_path):
    out = str(tmp_path / "fixtures")
    w = write_fixtures(jsonl, schema_file, out)
    assert len(w["fixtures"]) == 6

    res = run_fixtures(jsonl, out)
    assert res["holes"] == [], (
        "a negative fixture was not rejected for its stated reason: "
        + json.dumps(res["holes"], indent=2))
    assert res["ok"]
    assert all(r["outcome"] == "rejected-as-required" for r in res["results"])


def test_every_predicate_that_can_fail_is_covered_by_a_fixture(
        records, jsonl, schema_file, tmp_path):
    """A predicate no fixture probes is a predicate nobody has shown to work."""
    out = str(tmp_path / "fixtures")
    write_fixtures(jsonl, schema_file, out)
    index = json.loads((tmp_path / "fixtures" / FIXTURE_INDEX).read_text())
    covered = {f["target_predicate"] for f in index["fixtures"]}
    for required in ("fields_present", "identity_key", "classes_resolve",
                     "clock_resolvable", "declared_fields_unmasked"):
        assert required in covered, f"no fixture probes {required}"


def test_a_vacuous_validator_is_caught(tmp_path, jsonl, schema_file, monkeypatch):
    """Break the validator on purpose and confirm the harness notices.

    This is the non-vacuity check for the whole conformance idea. With
    `identity_candidates` neutered, every identity key measures as a session,
    so the bucket fixture should stop being rejected — and `conformance run`
    must report a HOLE rather than quietly succeeding.
    """
    import logschema.validate as V

    out = str(tmp_path / "fixtures")
    write_fixtures(jsonl, schema_file, out)

    real = V.identity_candidates

    def neutered(records, time_field, **kw):
        cands = real(records, time_field, **kw)
        for c in cands:
            c.verdict = "session"
            c.evidence = "neutered for the test"
        return cands

    monkeypatch.setattr(V, "identity_candidates", neutered)
    res = run_fixtures(jsonl, out)
    assert not res["ok"], "a validator that accepts everything was reported as fine"
    assert "identity-key-is-a-bucket" in res["holes"]
    hole = next(r for r in res["results"] if r["id"] == "identity-key-is-a-bucket")
    assert hole["outcome"] == "PASSED-BUT-SHOULD-FAIL"


def test_fixture_files_are_readable_yaml_with_a_reason_in_the_header(
        jsonl, schema_file, tmp_path):
    out = str(tmp_path / "fixtures")
    write_fixtures(jsonl, schema_file, out)
    idx = json.loads((tmp_path / "fixtures" / FIXTURE_INDEX).read_text())
    for fx in idx["fixtures"]:
        text = (tmp_path / "fixtures" / fx["file"]).read_text()
        assert "# FIXTURE:" in text
        assert "# MUST FAIL:" in text
        assert fx["target_predicate"] in text
        assert fx["why"][:30] in text.replace("\n", " ")
        # and it must still parse
        assert isinstance(load_schema(str(tmp_path / "fixtures" / fx["file"])), dict)


def test_a_missing_fixture_index_is_a_clear_error(jsonl, tmp_path):
    with pytest.raises(FileNotFoundError, match="conformance write"):
        run_fixtures(jsonl, str(tmp_path / "empty"))


# ---------------------------------------------------------------- round trip


def test_yaml_round_trips_through_the_dumper():
    from logschema.miniyaml import parse_yaml

    original = load_schema_str(GOOD)
    text = dump_yaml(original)
    again = parse_yaml(text)
    assert again == original, (
        "dump -> parse lost information; the loop's output would not be the "
        "loop's input")


def load_schema_str(text: str) -> dict:
    from logschema.miniyaml import parse_yaml

    return parse_yaml(text)


def test_dumper_quotes_anything_ambiguous():
    text = dump_yaml({"a": "has: colon", "b": "- leading dash",
                      "c": "", "d": 3, "e": True, "f": None})
    from logschema.miniyaml import parse_yaml

    got = parse_yaml(text)
    assert got["a"] == "has: colon"
    assert got["c"] == ""
    assert got["d"] == 3 and got["e"] is True and got["f"] is None


def test_dumper_keeps_a_list_of_scalars_on_one_line():
    text = dump_yaml({"identity_key": ["svc", "pid"]})
    assert "[" in text and "\n  -" not in text
    from logschema.miniyaml import parse_yaml

    assert parse_yaml(text)["identity_key"] == ["svc", "pid"]
