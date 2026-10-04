"""build_classifier: the consumer side of validated classes."""
import textwrap

from logschema.classify import build_classifier, content_field
from logschema.validate import load_schema

GOOD = textwrap.dedent("""\
    software: postfix
    identity_key: pid
    time_field: stamp
    record_layers:
      envelope:
        - {path: stamp, role: context, type: datetime, meaning: when}
        - {path: pid, role: identity, type: integer, meaning: process}
      payload:
        - {path: tok, role: content, type: string, meaning: the line}
    classes:
      - {name: connect, template: "connect from <HOST>", meaning: m, normal: true, retention: keep}
      - {name: sasl, template: "warning SASL LOGIN authentication failed", meaning: m, normal: false, retention: keep-extended}
    """)


def _schema(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(GOOD, encoding="utf-8")
    return load_schema(str(p))


def test_content_field_reads_role(tmp_path):
    assert content_field(_schema(tmp_path)) == "tok"


def test_classify_assigns_and_reports(tmp_path):
    schema = _schema(tmp_path)
    recs = [
        {"pid": "1", "stamp": "2026-01-01T00:00:00",
         "tok": "connect from unknown[1.2.3.4]"},
        {"pid": "1", "stamp": "2026-01-01T00:00:01",
         "tok": "warning: host[5.6.7.8]: SASL LOGIN authentication failed"},
        {"pid": "1", "stamp": "2026-01-01T00:00:02",
         "tok": "something entirely unrelated here"},
    ]
    classify, report = build_classifier(schema, recs)
    assert classify(recs[0]) == "connect"
    assert classify(recs[1]) == "sasl"
    assert classify(recs[2]) is None
    assert report["records_classified"] == 2
    assert report["records_unclassified"] == 1


def test_empty_template_is_the_empty_class(tmp_path):
    import copy
    schema = _schema(tmp_path)
    schema = copy.deepcopy(schema)
    schema["classes"].append({"name": "silence", "template": "",
                              "meaning": "m", "normal": True,
                              "retention": "demote"})
    classify, report = build_classifier(
        schema, [{"pid": "1", "stamp": "2026-01-01T00:00:00", "tok": ""}])
    assert classify({"tok": ""}) == "silence"
    assert report["empty_class"] == "silence"


def test_longest_template_wins_shared_shape(tmp_path):
    import copy
    schema = _schema(tmp_path)
    schema = copy.deepcopy(schema)
    schema["classes"].append(
        {"name": "connect_tls",
         "template": "connect from <HOST> STARTTLS",
         "meaning": "m", "normal": True, "retention": "keep"})
    recs = [{"pid": "1", "stamp": "2026-01-01T00:00:00",
             "tok": "connect from mx1 STARTTLS ready"}]
    classify, _ = build_classifier(schema, recs)
    assert classify(recs[0]) == "connect_tls"
