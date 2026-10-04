# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Syslog parsing, with the year handling pinned down by test.

The year logic exists because BSD syslog has no year, and it shipped one bug
in its first version: the wrap test compared the record against 1 January, so
every record after mid-February was dated to the previous year and a September
2026 file read as 2025. These tests are written to make that specific class of
error impossible to reintroduce silently, which is why several of them assert
on absolute dates rather than on "it parsed".
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from logschema import syslogfmt
from logschema.substrate import detect, read


def _write(tmp_path: Path, name: str, lines: list[str], mtime: datetime | None = None
           ) -> Path:
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime:
        import os
        os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    return p


# ----------------------------------------------------------------- sniffing

def test_sniff_identifies_bsd(tmp_path):
    p = _write(tmp_path, "maillog", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[17810]: connect from unknown[1.2.3.4]"])
    assert syslogfmt.sniff(p.read_bytes()) == "bsd"


def test_sniff_identifies_rfc5424(tmp_path):
    p = _write(tmp_path, "syslog.txt", [
        "<134>1 2026-09-27T03:27:45Z mx1 smtpd 17810 ID47 - connect from"])
    assert syslogfmt.sniff(p.read_bytes()) == "rfc5424"


def test_sniff_refuses_jsonl(tmp_path):
    """A JSONL export named syslog.txt must not be read as syslog."""
    p = _write(tmp_path, "syslog.txt", ['{"stamp": "2026-01-01T00:00:00", "a": 1}'])
    assert syslogfmt.sniff(p.read_bytes()) is None


def test_sniff_refuses_csv(tmp_path):
    p = _write(tmp_path, "x.log", ["stamp,message", "2026-01-01T00:00:00,hello"])
    assert syslogfmt.sniff(p.read_bytes()) is None


def test_space_padded_day_parses(tmp_path):
    """Real files pad single-digit days: 'Oct  3', two spaces."""
    p = _write(tmp_path, "maillog", [
        "Oct  3 20:44:15 mx1 postfix/smtpd[28046]: disconnect from unknown[1.2.3.4]"])
    rec = next(iter(read(detect(p), limit=1)))
    assert rec["ident"] == "postfix/smtpd"
    assert rec["pid"] == "28046"


# ------------------------------------------------------------------- fields

def test_native_field_names_only(tmp_path):
    """Syslog delimits stamp/host/ident/pid/message. Nothing else is derived.

    Notably absent: any program name derived from the tag. Measured on a
    300,000-line sample, taking the last path segment collapses
    `postfix/smtp` and `postfix/amavis/smtp` into one value -- a false merge,
    which is the one error class that cannot be undone downstream.
    """
    p = _write(tmp_path, "maillog", [
        "Sep 27 03:27:45 mx1 postfix/amavis/smtp[23378]: E7855142DE7B0: to=<a@b.c>"])
    rec = next(iter(read(detect(p), limit=1)))
    assert set(rec) == {"stamp", "host", "ident", "pid", "message"}
    assert rec["ident"] == "postfix/amavis/smtp"
    assert "svc" not in rec


def test_tag_without_pid_is_kept(tmp_path):
    """A tag with no [pid] is a record, not an error.

    Real: one roundcube webmail login line in 300,000. It carries a mailbox
    address, a client IP and a session id, so dropping it would be the worst
    possible place to drop one.
    """
    p = _write(tmp_path, "maillog", [
        "Sep 28 08:34:05 mx1 roundcube: <248912a9> Successful login for a@b.c"])
    rec = next(iter(read(detect(p), limit=1)))
    assert rec["ident"] == "roundcube"
    assert rec["pid"] == ""


def test_unparsed_lines_are_counted_not_dropped_silently(tmp_path):
    p = _write(tmp_path, "maillog", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: ok",
        "this line is not syslog at all",
        "Sep 27 03:27:46 mx1 postfix/smtpd[1]: ok",
    ])
    sub = detect(p)
    recs = list(read(sub, limit=10))
    assert len(recs) == 2
    assert sub.facts.unmatched == 1
    assert "not syslog" in sub.facts.sample_unmatched[0]


# --------------------------------------------------------------- the year

def test_year_from_rotation_suffix_beats_mtime(tmp_path):
    """A rotation job wrote that suffix when it closed the file."""
    p = _write(tmp_path, "maillog-20250927", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: ok"],
        mtime=datetime(2026, 10, 3, 20, 44))
    facts = syslogfmt.prepare(p, "bsd")
    assert facts.year == 2025
    assert facts.year_source == "rotation-suffix"


def test_year_is_always_flagged_as_assumed(tmp_path):
    """No source reads the year out of the records, so the flag never clears."""
    p = _write(tmp_path, "maillog-20250927", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: ok"])
    facts = syslogfmt.prepare(p, "bsd")
    assert facts.year_assumed is True


def test_records_after_mid_february_keep_the_reference_year(tmp_path):
    """The regression test for the shipped bug.

    The first implementation wrapped any record more than 45 days past
    1 January, so a log read in October dated itself to the previous year for
    every line from mid-February onward. Asserting an absolute date is the
    point: 'it parsed' would have passed while every timestamp was wrong.
    """
    p = _write(tmp_path, "maillog-20261003", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: early",
        "Oct  3 20:44:15 mx1 postfix/smtpd[2]: late",
    ])
    recs = list(read(detect(p), limit=10))
    assert recs[0]["stamp"] == "2026-09-27T03:27:45"
    assert recs[1]["stamp"] == "2026-10-03T20:44:15"


def test_new_year_wrap_dates_both_sides_correctly(tmp_path):
    """The case the year logic exists for: a file closed in January holding
    December records. Dated naively they land in the future, and a clock check
    reports a correctly ordered file as non-monotonic."""
    p = _write(tmp_path, "maillog-20260103", [
        "Dec 31 23:59:58 mx1 postfix/smtpd[1]: before",
        "Jan  1 00:00:03 mx1 postfix/smtpd[1]: after",
    ])
    recs = list(read(detect(p), limit=10))
    assert recs[0]["stamp"] == "2025-12-31T23:59:58"
    assert recs[1]["stamp"] == "2026-01-01T00:00:03"
    # Ordered, which is what a monotonicity check will look for.
    assert recs[0]["stamp"] < recs[1]["stamp"]


def test_far_from_reference_is_reported_as_ambiguous(tmp_path):
    """Past half a year from the closure date, two candidate years are equally
    defensible -- a month and a day only resolve a record to +/- 182 days -- so
    the format has run out of information. Counted, not guessed silently."""
    p = _write(tmp_path, "maillog-20260103", [
        "Jul 15 12:00:00 mx1 postfix/smtpd[1]: impossible",
    ])
    sub = detect(p)
    list(read(sub, limit=10))
    assert sub.facts.ambiguous == 1


def test_within_half_a_year_of_reference_is_not_ambiguous(tmp_path):
    """The other side of the tie point: 56 days out is a perfectly ordinary
    monthly log and must not be flagged."""
    p = _write(tmp_path, "maillog-20260103", [
        "Feb 28 12:00:00 mx1 postfix/smtpd[1]: ordinary",
    ])
    sub = detect(p)
    list(read(sub, limit=10))
    assert sub.facts.ambiguous == 0


def test_no_reference_date_yields_flagged_undated_records(tmp_path, monkeypatch):
    p = _write(tmp_path, "maillog", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: ok"])
    monkeypatch.setattr(syslogfmt, "reference_year", lambda _p: (None, "none"))
    facts = syslogfmt.prepare(p, "bsd")
    recs = list(syslogfmt.read(p, 10, facts))
    assert facts.undated == 1
    # An epoch date is honest: it is obviously not a real observation, so no
    # caller can mistake it for one.
    assert recs[0]["stamp"].startswith("1970-")


# ------------------------------------------------------------- integration

def test_detect_reports_syslog_kind_and_year_source(tmp_path):
    p = _write(tmp_path, "maillog", [
        "Sep 27 03:27:45 mx1 postfix/smtpd[1]: ok"])
    sub = detect(p)
    assert sub.kind == "syslog"
    d = sub.as_dict()
    assert d["syslog"]["year_assumed"] is True
    assert d["syslog"]["year_source"] in {"file-mtime", "rotation-suffix"}
    assert "year is not in the file" in d["note"]


def test_rfc5424_timestamps_pass_through_unchanged(tmp_path):
    """5424 carries a real timestamp with a year. We must not rewrite it."""
    ts = "2026-09-27T03:27:45.123456+02:00"
    p = _write(tmp_path, "syslog.txt", [f"<134>1 {ts} mx1 smtpd 17810 ID47 - hi"])
    rec = next(iter(read(detect(p), limit=1)))
    assert rec["stamp"] == ts
    assert rec["host"] == "mx1"
    assert rec["ident"] == "smtpd"
    assert rec["pid"] == "17810"
    assert rec["message"] == "hi"


def test_limit_is_respected(tmp_path):
    p = _write(tmp_path, "maillog", [
        f"Sep 27 03:27:{i:02d} mx1 postfix/smtpd[1]: n={i}" for i in range(50)])
    assert len(list(read(detect(p), limit=7))) == 7


def test_as_dict_is_json_serialisable(tmp_path):
    """inspect --json must not fall over on the new field."""
    p = _write(tmp_path, "maillog", ["Sep 27 03:27:45 mx1 a[1]: ok"])
    sub = detect(p)
    list(read(sub, limit=1))
    json.dumps(sub.as_dict())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
