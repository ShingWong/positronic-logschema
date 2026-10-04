# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Syslog: bytes in, the fields syslog itself delimits, out.

This module exists because of a measurement, not a plan. Every number we had
about a production Postfix log came from a JSONL file that an unshipped ad-hoc
script had produced. Re-run against the raw file on the server, that file turns
out to be plain RFC 3164 syslog, which the substrate was silently falling
through to a one-field `text` reader. A published schema that a user cannot run
against the file it was written for is not a schema, it is a screenshot.

So: two dialects, because both are in the wild and they are not the same shape.

  RFC 3164 (BSD)   Sep 27 03:27:45 mx1 postfix/smtpd[17810]: message
  RFC 5424         <134>1 2026-09-27T03:27:45Z mx1 smtpd 17810 - - message

## The year is not in the file

BSD syslog carries `MMM DD HH:MM:SS` and no year. Every timestamp we emit is
therefore a reconstruction, and a reconstructed timestamp that is silently
wrong is worse than no timestamp: it makes a monotonicity check pass on a file
that spans New Year. So the year comes from an explicitly named source, and the
source is reported on the substrate and in `inspect` output. If it cannot be
established, `year_assumed` stays true and callers are expected to say so out
loud rather than print a confident ISO date.

## What is deliberately NOT extracted

A Postfix queue id (`2C79E142DE806`) is the single most useful identity in a
mail log, and it is not extracted here. Syslog delimits a timestamp, a host, a
tag, a pid and a message; a queue id is a Postfix convention living inside that
message. Substrate parsing decides what the *format* delimits. Anything that
requires knowing which program wrote the line is schema meaning, and belongs to
the schema — where it can be declared, tested, and argued with.

The same rule is why `ident` is the raw tag and nothing more. Deriving a
program name from it by taking the last path segment is a lossy decision, and
measurement showed it loses real distinctions: on a 300,000-line sample
`postfix/smtp` and `postfix/amavis/smtp` both collapse to `smtp`. That is a
false merge, and a false merge is not recoverable downstream.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Fields the readers below emit. Native syslog names, verbatim.
STAMP, HOST, IDENT, PID, MESSAGE = "stamp", "host", "ident", "pid", "message"

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

# `Sep 27 03:27:45 mx1 postfix/smtpd[17810]: message`
# The day is space-padded in real files ("Oct  3"), which is why the separator
# is \s+ and not a single space. Measured: 300,000 of 300,000 lines matched.
_BSD = re.compile(
    r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}) (?P<hms>\d{2}:\d{2}:\d{2}) "
    r"(?P<host>\S+) (?P<ident>[^\s:]+?)(?:\[(?P<pid>\d+)\])?: ?(?P<msg>.*)$")

# `<134>1 2026-09-27T03:27:45.123Z mx1 smtpd 17810 ID47 - message`
_RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<ver>\d{1,2}) "
    r"(?P<ts>\S+) (?P<host>\S+) (?P<ident>\S+) (?P<pid>\S+) "
    r"(?P<msgid>\S+) (?P<sd>-|\[.*?\]) ?(?P<msg>.*)$")

# A rotation suffix, e.g. `maillog-20260927`. This names the day the file was
# rotated AWAY, which for a weekly rotation is within a few days of the last
# record in it -- close enough to name the year, which is all we need it for.
_ROTATION = re.compile(r"-(\d{4})(\d{2})(\d{2})$")

# How much closer the best candidate year must be than the runner-up before we
# call the assignment settled rather than a coin flip.
AMBIGUITY_MARGIN = 1.5


@dataclass
class SyslogFacts:
    """What we had to assume, and what we saw. Reported, never buried."""

    dialect: str = "bsd"          # bsd | rfc5424
    year: int | None = None
    year_source: str = "unknown"  # rotation-suffix | file-mtime | none
    year_assumed: bool = True
    reference: datetime | None = None   # when the file closed
    wrapped: int = 0              # records dated to a year other than the reference
    ambiguous: int = 0           # records whose year was near-enough a coin flip
    undated: int = 0             # records emitted with no year at all
    unmatched: int = 0            # lines that did not parse
    joined: int = 0               # continuation lines appended to a record
    sample_unmatched: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dialect": self.dialect,
            "year": self.year,
            "year_source": self.year_source,
            "year_assumed": self.year_assumed,
            "year_wrapped_records": self.wrapped,
            "year_ambiguous_records": self.ambiguous,
            "undated_records": self.undated,
            "unmatched_lines": self.unmatched,
            "joined_continuation_lines": self.joined,
            "unmatched_samples": self.sample_unmatched[:3],
        }


def sample(head: bytes, limit: int = 40) -> tuple[int, int, int, int]:
    """Vote across many lines, not just the first.

    Returns (bsd, rfc5424, other, continuation) over the first `limit`
    non-empty lines. One line is not evidence of a file: a JSON payload inside
    a syslog message, a wrapped line, or a mixed export can all make the first
    line lie. Callers require a quorum before naming a dialect, and conflicting
    dialects are reported as mixed rather than resolved by position.

    A non-matching line that FOLLOWS a syslog line is a continuation, not a
    vote against: tracebacks, wrapped lines and multi-line payloads carry no
    timestamp by nature. Only prose with no record before it counts as `other`.
    """
    bsd = rfc = other = cont = 0
    n = 0
    prev_syslog = False
    for line in head.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        n += 1
        if n > limit:
            break
        line = line.rstrip()
        if _RFC5424.match(line):
            rfc += 1
            prev_syslog = True
        elif _BSD.match(line):
            bsd += 1
            prev_syslog = True
        elif prev_syslog:
            cont += 1
        else:
            other += 1
            prev_syslog = False
    return bsd, rfc, other, cont


def sniff(head: bytes) -> str | None:
    """Which syslog dialect is this, if it is syslog at all?

    Decided on the bytes, never the extension. `maillog` has none, and a
    `syslog.txt` may be JSONL. Requires a quorum of the sampled lines: a single
    matching first line followed by anything else is not a syslog file.
    """
    bsd, rfc, other, _cont = sample(head)
    total = bsd + rfc + other
    if total == 0:
        return None
    if total < 3:
        # A one- or two-line file has no room for a quorum. If every counted
        # line matches one dialect, that is all the evidence there is.
        if bsd and not rfc and not other:
            return "bsd"
        if rfc and not bsd and not other:
            return "rfc5424"
        return None
    if bsd / total >= 0.8 and rfc == 0:
        return "bsd"
    if rfc / total >= 0.8 and bsd == 0:
        return "rfc5424"
    return None


def reference_year(path: Path) -> tuple[int | None, str]:
    """Establish the year BSD syslog omits. Returns (year, how-we-know).

    Two sources, in order of trustworthiness. A rotation suffix is written by
    the rotation job at the moment the file was closed, so it is a fact about
    the data. A file mtime is a fact about the filesystem, which survives a copy
    intact but is wrong the moment anyone touches the file. Neither is a fact
    about the records, and that is why the caller still reports the year as
    assumed either way.
    """
    m = _ROTATION.search(path.name)
    if m:
        return int(m.group(1)), "rotation-suffix"
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).year, "file-mtime"
    except OSError:
        return None, "none"


def _iso(ref: datetime | None, mon: int, day: int, hms: str,
         facts: SyslogFacts) -> str | None:
    """Assemble an ISO timestamp, choosing the year that puts it nearest `ref`.

    BSD syslog omits the year, so it has to be supplied. The naive approach --
    stamp everything with the file's year -- is wrong for any log that crosses
    New Year: December records land in the future and a monotonicity check
    then reports a correctly ordered file as disordered. The other naive
    approach, "wrap back a year if the month is early", is worse: it keys off
    the month alone, so every record after mid-February gets rolled back a year.
    That shipped for one run and dated a September 2026 log to 2025.

    So: try ref.year-1, ref.year and ref.year+1, and take whichever lands
    closest to the reference instant. That is correct for an ordinary file
    (all its records are within days of the reference) and correct across a
    New Year boundary, and it fails only where the format genuinely cannot
    help -- a file more than about six months from its own closure, where two
    candidates are equally plausible. Those are counted in `ambiguous` and
    reported, because a silent coin-flip on a year is the kind of error that
    surfaces months later as a story about a week that never happened.
    """
    try:
        hh, mm, ss = (int(x) for x in hms.split(":"))
        naive = datetime(2000, mon, day, hh, mm, ss)
    except ValueError:
        return None
    if ref is None:
        facts.undated += 1
        return naive.replace(year=1970).isoformat()

    ranked = sorted(
        (abs((naive.replace(year=y) - ref).total_seconds()),
         naive.replace(year=y))
        for y in (ref.year - 1, ref.year, ref.year + 1))
    gap, best = ranked[0]
    if best.year != ref.year:
        facts.wrapped += 1
    # Ambiguity is the MARGIN between the best year and the runner-up, not the
    # distance to the reference. An absolute tolerance cannot express it: the
    # closest a month-and-a-day can ever place a record is 182 days, so any
    # threshold past that is unreachable and any threshold below it fires on
    # ordinary files. What actually matters is how much better the winner is --
    # a file closed 3 January holding a 15 July record puts the two candidate
    # years 172 and 193 days out, which is a coin flip, while the same file
    # holding a 28 February record puts them 56 and 309 days out, which is not.
    if len(ranked) > 1 and gap > 0 and ranked[1][0] / gap < AMBIGUITY_MARGIN:
        facts.ambiguous += 1
    return best.isoformat()


def _emit_bsd(m, facts: SyslogFacts) -> dict:
    stamp = _iso(facts.reference, _MONTHS.get(m["mon"], 1),
                 int(m["day"]), m["hms"], facts)
    return {STAMP: stamp, HOST: m["host"], IDENT: m["ident"],
            PID: m["pid"] or "", MESSAGE: m["msg"]}


def read_bsd(path: Path, limit: int, facts: SyslogFacts) -> Iterator[dict]:
    """Yield BSD records, joining continuation lines into the record they follow.

    Wrapped lines, tracebacks and multi-line payloads do not start with a
    timestamp, so a strict one-line-per-record reader either drops them or
    counts them as unparsed. They belong to the previous record instead, which
    is a window of lines forming one record rather than one line each. A run of
    more than 200 continuations is flushed as-is: past that point this is a
    dump, not a record, and growing one string without bound is how a reader
    turns a corrupt file into an out-of-memory error.
    """
    with path.open(encoding="utf-8", errors="replace") as fh:
        n = 0
        current: dict | None = None
        runs = 0
        for line in fh:
            if n >= limit and current is None:
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = _BSD.match(line)
            if m:
                if current is not None:
                    n += 1
                    if n > limit:
                        return
                    yield current
                current = _emit_bsd(m, facts)
                runs = 0
            elif current is not None and runs < 200:
                current[MESSAGE] += "\n" + line.strip()
                facts.joined += 1
                runs += 1
            else:
                if current is not None:
                    n += 1
                    if n > limit:
                        return
                    yield current
                    current = None
                facts.unmatched += 1
                if len(facts.sample_unmatched) < 5:
                    facts.sample_unmatched.append(line[:160])
        if current is not None and n < limit:
            yield current


def read_rfc5424(path: Path, limit: int, facts: SyslogFacts) -> Iterator[dict]:
    with path.open(encoding="utf-8", errors="replace") as fh:
        n = 0
        current: dict | None = None
        runs = 0
        for line in fh:
            if n >= limit and current is None:
                break
            line = line.rstrip("\n")
            if not line.strip():
                continue
            m = _RFC5424.match(line)
            if m:
                if current is not None:
                    n += 1
                    if n > limit:
                        return
                    yield current
                current = {STAMP: m["ts"], HOST: m["host"], IDENT: m["ident"],
                           PID: m["pid"], MESSAGE: m["msg"]}
                runs = 0
            elif current is not None and runs < 200:
                current[MESSAGE] += "\n" + line.strip()
                facts.joined += 1
                runs += 1
            else:
                if current is not None:
                    n += 1
                    if n > limit:
                        return
                    yield current
                    current = None
                facts.unmatched += 1
                if len(facts.sample_unmatched) < 5:
                    facts.sample_unmatched.append(line[:160])
        if current is not None and n < limit:
            yield current


def prepare(path: Path, dialect: str) -> SyslogFacts:
    """Resolve everything knowable before a single record is read.

    Detection must be able to disclose the year reconstruction without having
    read the file, because `inspect` reports what it found before it starts
    streaming and a caveat that only appears once records flow is a caveat most
    callers never see.
    """
    facts = SyslogFacts(dialect=dialect)
    year, facts.year_source = reference_year(path)
    facts.year = year
    # The reference is an instant, not just a year: the distance test above is
    # only meaningful against a date. Rotation suffix wins over mtime because
    # it was written by the job that closed the file.
    m = _ROTATION.search(path.name)
    if m:
        try:
            facts.reference = datetime(int(m.group(1)), int(m.group(2)),
                                       int(m.group(3)))
        except ValueError:
            facts.reference = None
    if facts.reference is None and year is not None:
        try:
            facts.reference = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            facts.reference = None
    # A year from either source is a reconstruction. No source reads the year
    # out of the records themselves, so this stays True and is reported; the
    # flag is not a defect to fix but a property to show.
    return facts


def read(path: Path, limit: int, facts: SyslogFacts) -> Iterator[dict]:
    fn = read_bsd if facts.dialect == "bsd" else read_rfc5424
    yield from fn(path, limit, facts)
