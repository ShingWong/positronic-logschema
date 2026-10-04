"""`inspect` must measure, not assume.

The temptation in a tool like this is to special-case the corpora we happen to
have. Every test here is written against a synthetic file with a known answer
instead, so the code cannot quietly fit mx1 and pvl and then be wrong on
Windows or a Cisco capture.

The one place real corpora appear is `test_real_corpora.py`, and it asserts
verdicts we established by independent measurement rather than numbers this
code produced.
"""

from __future__ import annotations

import json

import pytest

from logschema.inspect_cmd import inspect_path, render
from logschema.profile import (
    find_time_field,
    skeleton,
)
from logschema.substrate import detect


def write(tmp_path, name, rows):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------- substrate


@pytest.mark.parametrize("name,body,kind", [
    ("a.jsonl", '{"x":1}\n{"x":2}\n', "jsonl"),
    ("a.ndjson", '{"x":1}\n{"x":2}\n', "jsonl"),
    # A .json extension on JSON Lines is common in log exports; trusting the
    # extension here yields zero records and a very confusing error later.
    ("a.json", '{"x":1}\n{"x":2}\n', "jsonl"),
    ("a.csv", "x,y\n1,2\n3,4\n", "delimited"),
    ("a.log", "hello world\nsecond line\n", "text"),
])
def test_detect_by_content_not_extension(tmp_path, name, body, kind):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    assert detect(p).kind == kind


def test_json_array_is_detected_and_counted(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([{"x": i} for i in range(50)]), encoding="utf-8")
    s = detect(p)
    assert s.kind == "json", f"misread as {s.kind}"
    assert s.available == 50, f"counted {s.available}, expected 50"
    assert s.truncated is False


def test_json_array_on_one_line_still_counts(tmp_path):
    p = tmp_path / "a.json"
    p.write_text(json.dumps([{"x": i} for i in range(50)]), encoding="utf-8")
    assert detect(p).available == 50


def test_empty_file_is_an_error_not_a_crash(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        detect(p)


def test_directory_is_refused_with_a_useful_message(tmp_path):
    with pytest.raises(IsADirectoryError, match="directory"):
        detect(tmp_path)


def test_limit_is_honoured_and_disclosed(tmp_path):
    rows = [{"ts": 1700000000 + i, "svc": "smtpd", "pid": i % 7,
             "msg": f"connect from host{i % 3}"} for i in range(500)]
    path = write(tmp_path, "big.jsonl", rows)
    r = inspect_path(path, limit=100)
    assert r["sampled"]["records_read"] == 100
    assert r["sampled"]["truncated"] is True
    assert "TRUNCATED" in render(r)


# ---------------------------------------------------------------- clock


def test_epoch_seconds_found_and_ranked(tmp_path):
    rows = [{"ts": 1700000000 + i * 60, "who": f"v{i % 40}"} for i in range(300)]
    r = inspect_path(write(tmp_path, "t.jsonl", rows))
    best = r["time_candidates"][0]
    assert best["field"] == "ts"
    assert best["parse_rate"] == 1.0
    assert best["monotonic"] == 1.0
    assert best["strategy"] == "epoch"


def test_epoch_millis_and_iso_and_naive_forms(tmp_path):
    for label, mk in [
        ("millis", lambda i: 1700000000000 + i * 60_000),
        ("iso", lambda i: f"2023-11-14T{i % 24:02d}:{i % 60:02d}:00Z"),
        ("naive", lambda i: f"2023-11-{14 + i % 10:02d} 12:00:00"),
    ]:
        rows = [{"t": mk(i), "k": f"k{i % 20}"} for i in range(200)]
        cands = find_time_field([{"t": r["t"]} for r in rows])
        assert cands, f"{label}: no clock found"
        assert cands[0].parse_rate == 1.0, label


def test_field_named_timestamp_that_is_not_a_clock_is_rejected(tmp_path):
    """A counter called `timestamp` parses as nothing; a jumbled id called
    `@time` that looks like a date parses but is not monotonic. Both must be
    ranked below a real clock rather than trusted by name."""
    rows = [{"real": 1700000000 + i * 60,
             "@time": f"2023-{(i * 7) % 12 + 1:02d}-{(i * 5) % 28 + 1:02d}T00:00:00Z",
             "n": i} for i in range(200)]
    path = write(tmp_path, "t.jsonl", rows)
    r = inspect_path(path)
    assert r["time_candidates"][0]["field"] == "real"


def test_no_clock_yields_a_question_not_a_crash(tmp_path):
    rows = [{"a": "x", "b": "y"} for _ in range(50)]
    r = inspect_path(write(tmp_path, "t.jsonl", rows))
    assert r["time_candidates"] == []
    clock = next(q for q in r["questions"] if q["id"] == "clock")
    assert clock["evidence"]["candidates"] == []
    assert "nothing to measure against" in clock["note"]


# ---------------------------------------------------------------- identity


def _verdicts(path, limit=200_000):
    r = inspect_path(path, limit=limit)
    return {tuple(c["fields"]): c["verdict"] for c in r["identity_candidates"]}


def test_session_key_is_called_a_session(tmp_path):
    """Bursts inside a day, many events per key -> session."""
    rows = []
    for sess in range(40):
        for k in range(60):
            rows.append({"pid": sess,
                         "ts": 1700000000 + sess * 86400 + k * 2,
                         "msg": f"line {k}"})
    v = _verdicts(write(tmp_path, "s.jsonl", rows))
    assert v[("pid",)] == "session"


def test_entity_key_is_called_an_entity(tmp_path):
    """Same events per key, spread across months -> entity, not session."""
    rows = []
    for who in range(60):
        for visit in range(4):
            rows.append({"who": who,
                         "ts": 1700000000 + who * 100000 + visit * 86400 * 40,
                         "room": visit % 3})
    v = _verdicts(write(tmp_path, "e.jsonl", rows))
    assert v[("who",)] == "entity"


def test_high_volume_category_is_a_bucket_not_an_entity(tmp_path):
    """The failure that matters: a domain or path lifted out of text recurs
    constantly and denotes nothing worth a record."""
    rows = [{"host": f"host{i % 4}", "msg": "x", "n": i,
             "ts": 1700000000 + i} for i in range(4000)]
    v = _verdicts(write(tmp_path, "b.jsonl", rows))
    assert v[("host",)] == "bucket"
    cand = next(c for c in inspect_path(write(tmp_path, "b.jsonl", rows))
                ["identity_candidates"] if tuple(c["fields"]) == ("host",))
    assert "category, not a thing" in cand["evidence"]


def test_unique_per_record_field_is_noise(tmp_path):
    rows = [{"uid": i, "ts": 1700000000 + i} for i in range(300)]
    v = _verdicts(write(tmp_path, "n.jsonl", rows))
    assert v[("uid",)] == "noise"


def test_redacted_field_is_refused_as_an_identity_key(tmp_path):
    """Every redacted record shares one value. Keying on it collapses the
    corpus into a single identity and reports it as fact."""
    rows = [{"uid": f"<HEX{i % 3}>", "real": f"u{i % 50}",
             "ts": 1700000000 + i} for i in range(300)]
    r = inspect_path(write(tmp_path, "r.jsonl", rows))
    uid = next(c for c in r["identity_candidates"] if tuple(c["fields"]) == ("uid",))
    assert uid["verdict"] == "noise"
    assert "redaction placeholders" in uid["evidence"]
    assert r["data_health"]["masked_fields"], "redaction not surfaced"
    q = next(x for x in r["questions"] if x["id"] == "redaction")
    assert "confident wrong answer" in q["note"]


def test_composite_key_offered_when_singles_collide(tmp_path):
    """Two services reusing pids: pid alone looks like a fine identifier until
    the same value appears under both."""
    rows = []
    for svc in ("smtpd", "qmgr"):
        for pid in range(30):
            for k in range(5):
                rows.append({"svc": svc, "pid": pid,
                             "ts": 1700000000 + pid * 100 + k})
    r = inspect_path(write(tmp_path, "c.jsonl", rows))
    keys = {frozenset(c["fields"]) for c in r["identity_candidates"]}
    assert frozenset({"svc", "pid"}) in keys, (
        f"composite key not offered; got {sorted(sorted(k) for k in keys)}")
    # The composite must beat both of its halves, or offering it is pointless.
    by = {frozenset(c["fields"]): c for c in r["identity_candidates"]}
    pair, pid, svc = by[frozenset({"svc", "pid"})], by[frozenset({"pid"})], \
        by[frozenset({"svc"})]
    assert pair["groups"] >= pid["groups"], "composite is not more selective"
    assert pid["verdict"] == "session", (
        f"pid should be a session on its own, got {pid['verdict']}")


def test_identity_question_lists_rejections_with_reasons(tmp_path):
    rows = [{"host": f"h{i % 3}", "uid": i, "who": f"w{i % 20}",
             "ts": 1700000000 + i} for i in range(500)]
    r = inspect_path(write(tmp_path, "q.jsonl", rows))
    q = next(x for x in r["questions"] if x["id"] == "identity")
    measured = q["evidence"]["measured"]
    assert len(measured) >= 2
    assert all("evidence" in m and m["evidence"] for m in measured)
    assert set(q["evidence"]["verdict_meaning"]) >= {"session", "entity", "bucket"}


# ---------------------------------------------------------------- classes


def test_templates_collapse_protocol_lines():
    assert skeleton("connect from 10.0.0.1[I:192.168.1.1]:443") == "connect from"
    # The queue id and its `Q:` prefix are one construct. Leaving a bare `Q`
    # behind makes an unrelated template look like a match.
    assert skeleton("Q:ABC123: removed") == "removed"
    assert skeleton("Q:4XyZhT2NnWqZ: from=<a@b.com>, size=1234, nrcpt=2 "
                    "(queue active)") == "from size nrcpt queue active"
    assert skeleton("lost connection after AUTH from [10.0.0.1]") == \
        "lost connection after AUTH from"


def test_free_text_is_untemplatable_not_a_class():
    """The pvl lesson: `WEST WING TOUR` appearing 45% of the time is not a
    template, it is a string someone typed. A 9-word sentence is prose."""
    assert skeleton("the quarterly planning meeting in the main boardroom") is None
    assert skeleton("") is None


def test_class_mining_reports_the_untemplatable_share(tmp_path):
    rows = [{"ts": 1700000000 + i,
             "d": "the quarterly planning meeting was moved again" if i % 3
                  else "connect from 10.0.0.1"}
            for i in range(300)]
    r = inspect_path(write(tmp_path, "cl.jsonl", rows))
    cl = r["classes"]
    assert cl["untemplatable_share"] > 0.5, "prose not detected"
    q = next(x for x in r["questions"] if x["id"] == "classes")
    assert "free-form" in q["note"], (
        "a corpus that is mostly untemplatable must be flagged as free-form, "
        "because a class inventory built from it just lists distinct values")
    assert "template corpus" in q["question"]


# ---------------------------------------------------------------- output


def test_json_output_is_machine_readable(tmp_path, capsys):
    from logschema.cli import main

    rows = [{"ts": 1700000000 + i, "pid": i % 5, "m": f"line {i}"} for i in range(50)]
    path = write(tmp_path, "j.jsonl", rows)
    assert main(["inspect", path, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tool"] == "logschema"
    assert out["identity_candidates"]


def test_inspect_never_blocks_and_exits_2_on_bad_path(tmp_path, capsys):
    from logschema.cli import main

    assert main(["inspect", str(tmp_path / "nope.jsonl")]) == 2
    assert "logschema:" in capsys.readouterr().err


def test_render_mentions_truncation_and_questions(tmp_path):
    rows = [{"ts": 1700000000 + i, "k": i % 4, "m": f"l{i}"} for i in range(50)]
    out = render(inspect_path(write(tmp_path, "x.jsonl", rows), limit=10))
    assert "QUESTIONS FOR THE USER" in out
    assert "TRUNCATED" in out
    assert "Nothing above is a schema" in out


# ------------------------------------------------- content selection

def test_content_prefers_shared_vocabulary_over_volume():
    """pvl `who` has 16 bytes on every record and zero shared terms; `desc`
    has prose on 8% of records. Volume ranking picks the id field and the
    class miner reports 0 shapes at 100% untemplatable -- which is what
    shipped once, so this test names the corpus."""
    from logschema.profile import FieldProfile, content_candidates
    def fp(name, **kw):
        d = {"fill": (1.0, 1), "reuse": 0.5, "toks": 3.0, "distinct": 100,
             "kind": "str"}
        d.update(kw)
        p = FieldProfile(name=name)
        p.non_null, p.present = 100, 100
        p.mean_len = 10.0
        p.vocab_reuse, p.mean_tokens = d["reuse"], d["toks"]
        p.distinct = d["distinct"]
        p.types["str"] = 100
        return p
    got = [f.name for f in content_candidates({
        "who": fp("who", reuse=0.03, toks=1.0, distinct=95),
        "desc": fp("desc", reuse=0.92, toks=2.8, distinct=45),
    })]
    assert got[0] == "desc"


def test_content_demotes_enums_below_prose():
    """pvl `loc`: 8 codes, reuse 1.00, zero descriptive power. Perfect reuse
    must not beat actual prose."""
    from logschema.profile import FieldProfile, content_candidates
    def fp(name, **kw):
        p = FieldProfile(name=name)
        p.non_null, p.present = 100, 100
        p.mean_len = 10.0
        p.types["str"] = 100
        for k, v in kw.items():
            setattr(p, k, v)
        return p
    got = [f.name for f in content_candidates({
        "loc": fp("loc", vocab_reuse=1.0, mean_tokens=1.0, distinct=8),
        "desc": fp("desc", vocab_reuse=0.9, mean_tokens=3.0, distinct=45),
    })]
    assert got == ["desc", "loc"]


def test_content_token_count_beats_clock_reuse():
    """mx1 `stamp`: reuse 1.00 (digits repeat), 5 tokens. The message it
    timestamps has reuse 0.98 and a dozen tokens. Reuse alone picks the
    clock."""
    from logschema.profile import FieldProfile, content_candidates
    def fp(name, **kw):
        p = FieldProfile(name=name)
        p.non_null, p.present = 100, 100
        p.mean_len = 10.0
        p.types["str"] = 100
        for k, v in kw.items():
            setattr(p, k, v)
        return p
    got = [f.name for f in content_candidates({
        "stamp": fp("stamp", vocab_reuse=0.998, mean_tokens=5.0,
                     distinct=77),
        "message": fp("message", vocab_reuse=0.980, mean_tokens=12.8,
                       distinct=400),
    })]
    assert got[0] == "message"


def test_vocab_reuse_separates_ids_from_prose():
    from logschema.profile import _vocab_reuse
    reuse_ids, _ = _vocab_reuse([f"{i:016x}" for i in range(300)])
    reuse_prose, toks = _vocab_reuse(["morning meeting in hall",
                                      "evening meeting in hall"] * 100)
    assert reuse_ids < 0.1
    assert reuse_prose > 0.5
    assert toks == 4.0
