# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""What is in the file: fields, types, time, content, identity, classes.

Everything here is measurement. Nothing infers intent. That split is the
whole reason this module exists separately from the model that drafts a schema:
the model's contribution is vocabulary and semantics, and its weakness is
exactly the arithmetic below — which field is the key, which values are real
identifiers, whether a timestamp can be trusted. Measured here, it is decided
by the data rather than asserted.

The identity measurement in particular exists because a language model asked
"which field identifies an SMTP session" has been observed to answer "no
single field does, correlate pid with the client address" while in the same
breath reasoning that the daemon forks one child per connection — which means
pid *is* the session key. `identity_candidates` settles that by counting
groups, and the answer is the same whichever way the model leans.
"""

from __future__ import annotations

import itertools
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# Placeholders a redaction pass leaves behind. A schema that treats one of
# these as an identifier silently collapses every redacted record onto a single
# identity, and then reports one session for a whole corpus. This is the one
# failure that produces a confident wrong answer instead of an error.
PLACEHOLDER = re.compile(
    r"(<[A-Z][A-Z0-9_]{1,20}>|\[[A-Z]{3,12}\]|\*{3,}|x{3,}|"
    r"REDACTED|MASKED|ANONYMISED|ANONYMIZED)"
)
IP_LIKE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
HEX_LIKE = re.compile(r"^[0-9a-fA-F]{6,}$")
EMAIL_LIKE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PATH_LIKE = re.compile(r"^(/|\./|\.\./|[A-Za-z]:\\)")
UUID_LIKE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-", re.IGNORECASE)
DIGITS = re.compile(r"^\d+$")

# Epoch bounds, seconds. Below 1e8 is 1973; above 4e9 is 2106.
EPOCH_LO, EPOCH_HI = 100_000_000, 4_000_000_000


@dataclass
class FieldProfile:
    name: str
    present: int = 0
    non_null: int = 0
    distinct: int = 0
    types: Counter = field(default_factory=Counter)
    max_len: int = 0
    mean_len: float = 0.0
    placeholder_rate: float = 0.0
    samples: list = field(default_factory=list)

    @property
    def fill_rate(self) -> float:
        return self.non_null / self.present if self.present else 0.0

    @property
    def cardinality(self) -> float:
        return self.distinct / self.non_null if self.non_null else 0.0

    @property
    def kind(self) -> str:
        """Dominant value shape. Reported as a measurement, never as a role."""
        if not self.types:
            return "unknown"
        top, _count = self.types.most_common(1)[0]
        if top == "null":
            rest = [(t, c) for t, c in self.types.items() if t != "null"]
            if not rest:
                return "always-null"
            top, _n = max(rest, key=lambda kv: kv[1])
        return top

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "present": self.present,
            "non_null": self.non_null,
            "fill_rate": round(self.fill_rate, 4),
            "distinct": self.distinct,
            "cardinality": round(self.cardinality, 4),
            "mean_len": round(self.mean_len, 1),
            "max_len": self.max_len,
            "placeholder_rate": round(self.placeholder_rate, 4),
            "samples": self.samples,
        }


def _vtype(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (list, dict)):
        return "structured"
    s = str(v).strip()
    if s == "":
        return "empty-string"
    return "str"


def profile_fields(records: Iterable[dict]) -> dict[str, FieldProfile]:
    profs: dict[str, FieldProfile] = {}
    total = 0
    for rec in records:
        total += 1
        for k, v in rec.items():
            fp = profs.get(k)
            if fp is None:
                fp = profs[k] = FieldProfile(name=k)
            fp.present += 1
            fp.types[_vtype(v)] += 1
            if v is None:
                continue
            s = v if isinstance(v, str) else str(v)
            if s == "":
                fp.types["empty-string"] += 1
                continue
            fp.non_null += 1
            fp.mean_len += len(s)
            fp.max_len = max(fp.max_len, len(s))
            if PLACEHOLDER.search(s):
                fp.placeholder_rate += 1
            if len(fp.samples) < 400:
                fp.samples.append(s[:120])
    for fp in profs.values():
        if fp.non_null:
            fp.mean_len /= fp.non_null
        fp.placeholder_rate = fp.placeholder_rate / fp.non_null if fp.non_null else 0.0
        fp.distinct = len(set(fp.samples)) if fp.samples else 0
        # distinct is computed from capped samples above only as a fallback;
        # recompute exactly for small fields via the caller's cardinality pass.
    return profs


# ---------------------------------------------------------------- time


@dataclass
class TimeCandidate:
    field: str
    strategy: str
    parse_rate: float = 0.0
    monotonic: float = 0.0
    span: timedelta | None = None
    first: datetime | None = None
    last: datetime | None = None
    reason: str = ""

    @property
    def score(self) -> float:
        """Parseability dominates; monotonicity breaks ties.

        A field that parses 100% of the time but jumps around is not a clock.
        A field that parses perfectly and never goes backwards is, with very
        little room for argument.
        """
        return self.parse_rate * (0.5 + 0.5 * self.monotonic)

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "strategy": self.strategy,
            "parse_rate": round(self.parse_rate, 4),
            "monotonic": round(self.monotonic, 4),
            "score": round(self.score, 4),
            "span_days": round(self.span.total_seconds() / 86400, 3) if self.span else None,
            "first": self.first.isoformat() if self.first else None,
            "last": self.last.isoformat() if self.last else None,
        }


def _parse_epoch(v) -> datetime | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    a = abs(f)
    if EPOCH_LO <= a <= EPOCH_HI:            # seconds
        return datetime.fromtimestamp(f, tz=timezone.utc)
    if 1e11 <= a <= 4e12:                     # milliseconds
        return datetime.fromtimestamp(f / 1000.0, tz=timezone.utc)
    if 1e14 <= a <= 4e15:                     # microseconds
        return datetime.fromtimestamp(f / 1e6, tz=timezone.utc)
    if 1e17 <= a <= 4e18:                     # nanoseconds
        return datetime.fromtimestamp(f / 1e9, tz=timezone.utc)
    return None


_ISO_TRAIL = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _parse_iso(v) -> datetime | None:
    s = str(v).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_slashed(v) -> datetime | None:
    """`YYYY-MM-DD hh:mm:ss` with optional fractional seconds and offset."""
    s = str(v).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dmy(v) -> datetime | None:
    """`DD/MM/YYYY hh:mm:ss`, still common in European syslog deployments."""
    s = str(v).strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_us(v) -> datetime | None:
    """`14/Nov/2023:22:13:20 +0000`, the Apache/Postfix common-log format."""
    s = str(v).strip()
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%d/%b/%Y %H:%M:%S",
                "%b %d %H:%M:%S", "%b %d %H:%M:%S %Y"):
        try:
            # %z formats come back aware; the rest are declared UTC rather
            # than left naive, so every comparison downstream has a tz.
            dt = datetime.strptime(s, fmt)  # noqa: DTZ007 - tz added below
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# Tried in order. `fromisoformat` absorbs the space-separated and offset forms
# that a strptime pair used to cover, and every parser here returns None on
# anything it does not understand — a parser that raises is a parser that turns
# one odd field into a crash of the whole inspection.
_STR_FORMATS = (
    ("iso8601", _parse_iso),
    ("epoch", _parse_epoch),
    ("slash-date", _parse_slashed),
    ("day-month-year", _parse_dmy),
    ("apache-common", _parse_us),
)


def find_time_field(records: list[dict], limit: int = 20_000) -> list[TimeCandidate]:
    """Rank fields by how well they behave as a clock.

    Deliberately scored on behaviour, not on the field being named `ts` or
    `time` or `stamp`. A log with a field called `timestamp` holding a
    sequence counter, or `@timestamp` holding a formatted date, both defeat a
    name-based guess; neither defeats this.
    """
    sample = records[:limit]
    out: list[TimeCandidate] = []
    for name in {k for r in sample for k in r}:
        values = [r.get(name) for r in sample if r.get(name) not in (None, "")]
        if len(values) < max(10, len(sample) * 0.5):
            continue
        for strategy, parse in _STR_FORMATS:
            parsed = [parse(v) for v in values]
            ok = [d for d in parsed if d is not None]
            rate = len(ok) / len(values)
            if rate < 0.9:
                continue
            mono = _monotonic_fraction(ok)
            cand = TimeCandidate(field=name, strategy=strategy, parse_rate=rate,
                                 monotonic=mono)
            cand.first, cand.last = min(ok), max(ok)
            cand.span = cand.last - cand.first
            if mono >= 0.99:
                cand.reason = "parses and never goes backwards"
            elif mono >= 0.9:
                cand.reason = "parses; small number of out-of-order records"
            else:
                cand.reason = ("parses but jumps around — probably not the "
                               "event clock")
            out.append(cand)
            break
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def _monotonic_fraction(seq: list[datetime]) -> float:
    if len(seq) < 2:
        return 1.0
    ok = sum(1 for a, b in itertools.pairwise(seq) if b >= a)
    return ok / (len(seq) - 1)


# ---------------------------------------------------------------- identity


@dataclass
class IdentityCandidate:
    fields: tuple[str, ...]
    groups: int = 0
    singletons: int = 0
    mean_size: float = 0.0
    p95_size: float = 0.0
    span: timedelta | None = None        # corpus span for this candidate
    span_days: float | None = None
    group_span_days: float | None = None  # MEDIAN per-key span: the real signal
    group_rate: float | None = None      # MEDIAN per-key events per day
    median_gap_s: float | None = None
    rate_per_day: float | None = None
    placeholder_rate: float = 0.0
    shape: str = "unknown"
    verdict: str = "unknown"
    evidence: str = ""

    def as_dict(self) -> dict:
        return {
            "fields": list(self.fields),
            "groups": self.groups,
            "singletons": self.singletons,
            "mean_size": round(self.mean_size, 2),
            "p95_size": round(self.p95_size, 2),
            "span_days": round(self.span_days, 3) if self.span_days is not None else None,
            "group_span_days": (round(self.group_span_days, 4)
                                if self.group_span_days is not None else None),
            "group_rate_per_day": (round(self.group_rate, 3)
                                   if self.group_rate is not None else None),
            "median_gap_s": (round(self.median_gap_s, 2)
                             if self.median_gap_s is not None else None),
            "rate_per_day": (round(self.rate_per_day, 3)
                             if self.rate_per_day is not None else None),
            "placeholder_rate": round(self.placeholder_rate, 4),
            "shape": self.shape,
            "verdict": self.verdict,
            "evidence": self.evidence,
        }


def coverage_is_partial(
    records: list[dict], time_field: str | None, hit_limit: bool = False
) -> tuple[bool, float, str]:
    """Is this scan the whole corpus, or did the reader stop early?

    A partial scan is structurally incapable of showing that a key recurs.
    Measured on pvl: the first 60,000 of 331,147 rows contain 58,787 distinct
    visitors with a mean of 1.02 events each, so `who` reads as noise — while
    over the whole file it is 99,987 visitors at 3.31 events each, and reads as
    an entity. Nothing about the data changed; only the window did.

    So identity verdicts from a partial scan are not verdicts, they are lower
    bounds, and the caller has to be told.

    The test is whether the reader exhausted the file, not anything about
    timestamps. An earlier version compared the last record's timestamp against
    the latest one in the window and concluded pvl was fully scanned, because
    pvl is not sorted by time — the 60,000th row is not the newest row. Asking
    the reader whether it stopped is exact, needs no ordering assumption, and
    cannot be fooled by an unsorted file.
    """
    if hit_limit:
        return True, 1.0, (
            "the reader stopped at the record limit, so this is a prefix of "
            "the file and repeats of any key may fall beyond it")
    if not records:
        return False, 0.0, ""
    return False, 1.0, "the reader reached end of file"


def identity_candidates(
    records: list[dict],
    time_field: str | None,
    max_candidates: int = 12,
    partial_coverage: bool = False,
) -> list[IdentityCandidate]:
    """Measure every plausible identity key and return them ranked.

    The verdict vocabulary is deliberately four-way, because the useful
    distinction is not entity-versus-not. It is:

      session — few events per key, clustered tightly in time. A conversation.
      entity  — few events per key, spread out. A person, a host, a mailbox.
      bucket  — a huge number of events per key. A category, not a thing.
      noise   — nearly every key seen once. Not an identifier at all.

    `bucket` is the one that matters most to get right, and it is the one a
    schema author is most likely to accept: a domain or a path extracted from
    a text field looks like a name, recurs many times, and denotes nothing
    that anyone would want a record about.

    With `partial_coverage` set, `noise` becomes `undetermined` rather than an
    answer. See `coverage_is_partial` for why that distinction is load-bearing
    rather than cautious.
    """
    if not records:
        return []
    names = sorted({k for r in records for k in r})
    shapes: dict[str, str] = {}
    for n in names:
        shapes[n] = _value_shape(records, n)

    cands: list[IdentityCandidate] = []
    for n in names:
        cands.append(_measure(records, (n,), time_field, shapes.get(n, "unknown"),
                              partial_coverage))

    # Composites, because the real key is often a pair. A (service, pid) key
    # is the common case in daemon logs where pids are reused across services,
    # and a single pid field looks like a perfectly good identifier right up
    # until it collides.
    # Composite candidates, because the real key is often a pair. A
    # (service, pid) key is the common case in daemon logs where pids are
    # reused across services, and a bare pid field looks like a perfectly good
    # identifier right up until it collides.
    #
    # Only genuinely plausible singles are paired. Ranking by mean group size
    # keeps the huge categorical fields — svc, host, facility — out of the
    # pairs, since a composite of two categories is a coarser category and
    # tells the user nothing they did not already have.
    # `bucket` fields are INCLUDED here even though they are rejected as keys in
    # their own right. A low-cardinality categorical field is exactly the
    # partition half of a composite key — `pid` alone collides across services,
    # and (svc, pid) is the pair that identifies a Postfix session. Excluding
    # buckets from the pool is what stopped the most useful composite we know
    # how to build from ever being generated.
    singles = [c for c in cands if len(c.fields) == 1
               and c.verdict != "noise"
               and c.mean_size < 1000 and c.placeholder_rate < 0.5]
    # Rank by how *selective* a field is: the closer its group count is to the
    # record count, the closer it is to being an identifier. Ranking by
    # mean_size put the widest categorical fields first, which is how (svc, pid)
    # fell outside the top four.
    singles.sort(key=lambda c: (-c.groups / max(1, len(records)), c.fields[0]))
    for i, a in enumerate(singles[:4]):
        for b in singles[:4]:
            if a.fields[0] >= b.fields[0]:
                continue
            pair = (a.fields[0], b.fields[0])
            cands.append(_measure(records, pair, time_field, "composite",
                                  partial_coverage))
    cands.sort(key=lambda c: (_rank(c.verdict), -c.mean_size))
    return cands[:max_candidates]


# Ordering for display. `undetermined` sorts with `unknown` rather than with
# `noise`: it is an absence of evidence, and placing it beside the negative
# verdict is what caused a real entity field to be written off.
_RANK = {"session": 0, "entity": 1, "bucket": 2, "undetermined": 3,
         "unknown": 3, "noise": 4}


def _rank(verdict: str) -> int:
    return _RANK.get(verdict, 3)


def _measure(records: list[dict], fields: tuple[str, ...],
             time_field: str | None, shape: str,
             partial_coverage: bool = False) -> IdentityCandidate:
    groups: dict[tuple, list] = defaultdict(list)
    n_ph = 0
    n_val = 0
    first_seen: list = []
    last_seen: list = []
    for r in records:
        key = tuple(str(r.get(f)) for f in fields)
        if any(k in ("None", "") for k in key):
            continue
        if any(PLACEHOLDER.search(k) for k in key):
            n_ph += 1
        n_val += 1
        groups[key].append(r)
        if time_field:
            for _s, parse in _STR_FORMATS:
                d = parse(r.get(time_field)) if r.get(time_field) is not None else None
                if d is not None:
                    first_seen.append(d)
                    last_seen.append(d)
                    break

    sizes = sorted(len(v) for v in groups.values())
    singletons = sum(1 for s in sizes if s == 1)
    mean_size = (n_val / len(groups)) if groups else 0.0
    p95 = sizes[int(0.95 * (len(sizes) - 1))] if sizes else 0

    gaps: list[float] = []
    span: timedelta | None = None
    rate: float | None = None
    # Per-key spans and rates. These, not the corpus span, are what separate a
    # session from an entity: a session's events fall inside minutes, so its
    # own span is short even though the corpus covers months. Averaging over the
    # corpus instead would call a 132-line SMTP conversation an entity.
    group_spans: list[float] = []
    group_rates: list[float] = []
    if time_field and groups:
        times: list[datetime] = []
        for rows in groups.values():
            ds = []
            for r in rows:
                for _strategy, parse in _STR_FORMATS:
                    d = parse(r.get(time_field)) if r.get(time_field) is not None else None
                    if d is not None:
                        ds.append(d)
                        break
            if not ds:
                continue
            ds.sort()
            times.extend(ds)
            if len(ds) >= 2:
                gaps.extend((b - a).total_seconds() for a, b in itertools.pairwise(ds))
                own = (ds[-1] - ds[0]).total_seconds()
                group_spans.append(own / 86400.0)
                if own > 0:
                    group_rates.append(len(ds) / (own / 86400.0))
        if times:
            span = max(times) - min(times)
            if span and span.total_seconds() > 0:
                rate = len(times) / (span.total_seconds() / 86400.0)

    c = IdentityCandidate(
        fields=fields, groups=len(groups), singletons=singletons,
        mean_size=mean_size, p95_size=p95, span=span,
        span_days=(span.total_seconds() / 86400.0) if span else None,
        group_span_days=(statistics.median(group_spans) if group_spans else None),
        group_rate=(statistics.median(group_rates) if group_rates else None),
        median_gap_s=(statistics.median(gaps) if gaps else None),
        rate_per_day=rate,
        placeholder_rate=(n_ph / n_val if n_val else 0.0),
        shape=shape,
    )
    c.verdict, c.evidence = _classify(c, partial_coverage)
    return c


def _classify(c: IdentityCandidate, partial: bool = False) -> tuple[str, str]:
    if c.groups == 0:
        return "noise", "no non-empty values"
    if c.shape == "free-text" and c.mean_size < 5:
        return "noise", ("values are prose; distinct texts rather than "
                         "identifiers")
    if c.placeholder_rate > 0.5:
        return "noise", (f"{c.placeholder_rate:.0%} of values are redaction "
                         f"placeholders — this field cannot be an identity key")
    frac_single = c.singletons / c.groups
    if frac_single > 0.95:
        if partial:
            # Not an answer. A key whose repeats fall outside the window cannot
            # be distinguished from a key that never repeats, and reporting it
            # as noise is how the one real entity field in a corpus gets
            # dismissed.
            return "undetermined", (
                f"{frac_single:.0%} of keys appear once in this window, but "
                f"the window is a prefix of the timeline so later repeats were "
                f"not visible — rescan without a limit before deciding")
        return "noise", (f"{frac_single:.0%} of keys appear exactly once, so "
                         f"the field identifies nothing that recurs")
    # The classifier reads `group_span_days`, the median span of a single key's
    # own events. A session's events land inside minutes; an entity's land
    # across months. Volume cannot decide this on its own — a Postfix smtpd
    # child averages 132 lines and is emphatically a session, while a `svc`
    # field with 15 values and 100,000 events each is emphatically a category.
    own = c.group_span_days
    if own is not None and c.span_days:
        # What separates a session from a category is not volume — a Postfix
        # smtpd child averages 132 lines and a 4-host test corpus averages
        # 1,000 events per host — it is whether a key's events are *clustered*
        # in the corpus timeline. A session is a thin slice of it. A category
        # is spread across all of it.
        ratio = own / c.span_days
        clustered = ratio < 0.05
        if clustered:
            return "session", (
                f"{c.mean_size:,.0f} events per key inside a median "
                f"{own * 1440:,.0f} minutes — {ratio:.1%} of the corpus "
                f"timeline, so a burst rather than a thread")
        if c.mean_size >= 50:
            return "bucket", (
                f"{c.mean_size:,.0f} events per key spread over "
                f"{ratio:.0%} of the corpus timeline — a category, not a "
                f"thing")
        return "entity", (
            f"{c.mean_size:,.1f} events per key spread over a median "
            f"{own:,.0f} days ({ratio:.0%} of the timeline) — a long-lived "
            f"thread")
    # No usable clock: fall back to volume, and say that is what happened.
    if c.mean_size >= 50:
        return "bucket", (f"{c.mean_size:,.0f} events per key over "
                          f"{c.groups:,} keys — a category, not a thing")
    if c.mean_size >= 2:
        return "entity", (f"{c.mean_size:,.1f} events per key, but no usable "
                          f"clock to separate a burst from a thread")
    return "noise", f"{frac_single:.0%} of keys appear once"


def _value_shape(records: list[dict], name: str, sample: int = 500) -> str:
    vals = []
    for r in records:
        v = r.get(name)
        if v in (None, ""):
            continue
        s = str(v).strip()
        if s:
            vals.append(s)
        if len(vals) >= sample:
            break
    if not vals:
        return "empty"
    if all(DIGITS.match(v) for v in vals):
        return "integer"
    if all(EMAIL_LIKE.match(v) for v in vals):
        return "email"
    if all(IP_LIKE.match(v) for v in vals):
        return "ipv4"
    if all(UUID_LIKE.match(v) for v in vals):
        return "uuid"
    if all(HEX_LIKE.match(v) for v in vals):
        return "hex"
    if all(PATH_LIKE.match(v) for v in vals):
        return "path"
    if all(" " not in v for v in vals):
        return "token"
    if all(v.isupper() for v in vals):
        return "code"
    lens = statistics.mean(len(v) for v in vals)
    return "free-text" if lens > 40 else "token"


# ---------------------------------------------------------------- content


def content_candidates(profs: dict[str, FieldProfile]) -> list[FieldProfile]:
    """Fields that could carry the text a person would want to read.

    Ranked by how much text they actually contribute, because that text is the
    only thing the consuming engine reads. A field that is 92% empty is not a
    content field no matter what it is called — measured here rather than
    trusted, because an ingestion harness that flattens every record to a
    single string is exactly the mistake that makes an engine look like it
    cannot find an entity that is plainly present in the data.
    """
    out = []
    for fp in profs.values():
        if fp.kind in ("int", "float", "bool"):
            continue
        if fp.fill_rate < 0.05:
            continue
        out.append(fp)
    out.sort(key=lambda f: (-f.mean_len * f.fill_rate, f.name))
    return out


# ---------------------------------------------------------------- classes


def mine_classes(
    records: list[dict],
    text_field: str | None,
    max_classes: int = 60,
    max_len: int = 120,
) -> tuple[list[tuple[str, int]], int, int]:
    """Return (top shapes, untemplatable count, TOTAL distinct shapes).

    The total is counted over every shape, not over the returned top N.
    Reporting `len(top)` as the distinct count would say a 1.5M-line mail log
    has 60 event classes, which is an artefact of the display cap being
    mistaken for the size of the vocabulary.
    """
    """Group records by the shape of their text with the values removed.

    This is the same normalisation used to discover that a 1.5M-line mail log
    is really only about ten thousand distinct events, and that a corpus of
    visitor descriptions is not a template corpus at all — its top two
    "templates" were the free-text strings `WEST WING TOUR` and `GROUP TOUR`.

    That second case is the reason the empty-class share is reported next to
    the counts: when most records cannot be templated, a class inventory built
    this way is not describing the data and the caller needs to know that
    before drawing conclusions from it.
    """
    if not text_field:
        return [], 0, 0
    shapes: Counter = Counter()
    total = 0
    untemplatable = 0
    for r in records:
        v = r.get(text_field)
        if v in (None, ""):
            continue
        total += 1
        s = skeleton(str(v), max_len)
        if s is None:
            untemplatable += 1
        else:
            shapes[s] += 1
    return shapes.most_common(max_classes), untemplatable, len(shapes)


# Order matters. Bracketed forms go before bare numbers so that `[I:1.2.3.4]:25`
# and `Q:4XyZhT2NnWqZ:` lose the whole construct rather than leaving the
# letter `I` behind as a word — a leftover `I` in the skeleton is the
# difference between `connect from` and `connect from I`.
_SKEL = (
    (re.compile(r"\[I:[^\]]*\](?::\d+)?"), " "),
    (re.compile(r"\[(\d{1,3}\.){3}\d{1,3}\](?::\d+)?"), " "),
    (re.compile(r"\[(\d{1,3}\.){3}\d{1,3}\]"), " "),
    (re.compile(r"\[(?:[A-Z]{3,12}|[\d.]+)\]"), " "),
    (re.compile(r"\bQ:[0-9A-Za-z]+:"), " "),
    (re.compile(r"<[^<>\s]{1,40}>"), " "),
    (re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b"), " "),
    (re.compile(r"\b(\d{1,3}\.){3}\d{1,3}\b"), " "),
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), " "),
    (re.compile(r"\b\d+(?:[.:,]\d+)*\b"), " "),
    (re.compile(r"\s+"), " "),
)

# Closed-class words. Used to tell a fixed log phrase from prose. Kept short on
# purpose: over-including is how a real template gets thrown away.
_FUNCTION_WORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to",
    "for", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "it", "its", "this", "that", "these", "those", "as", "into", "about",
})


def skeleton(text: str, max_len: int = 120) -> str | None:
    """Literal wording with the variable parts removed, or None if unusable.

    None means "this text is prose, not a template" — every free-text value
    gets its own shape, so keeping them would produce an inventory that is
    just a copy of the distinct values.
    """
    s = text.strip()
    if not s:
        return None
    if len(s) > max_len * 4:
        return None
    for pat, rep in _SKEL:
        s = pat.sub(rep, s)
    s = re.sub(r"[^A-Za-z0-9 ]+", " ", s)
    words = re.sub(r"\s+", " ", s).strip()
    if not words:
        return None
    # Prose is not a template, and word count alone is the wrong test:
    # "from=<a>, size=, nrcpt= (queue active)" masks down to 5 words and is
    # unambiguously a template. The discriminator is grammatical function
    # words — a template is a fixed phrase ("lost connection after AUTH from"),
    # while a sentence someone typed carries articles and prepositions that a
    # machine-generated line essentially never has.
    toks = words.split()
    if len(toks) > 14:
        return None
    if not toks:
        return None
    # Two function words is the line. One is fine — "lost connection after AUTH
    # from" and "connect from" both contain "from" and are real templates. Two
    # means the text is a sentence: "the quarterly planning meeting was moved
    # again" has the, was and is not a shape anyone can match.
    if sum(1 for t in toks if t.lower() in _FUNCTION_WORDS) >= 2:
        return None
    return words[:max_len]


def jsonable(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, Counter):
        return dict(obj)
    raise TypeError(type(obj))
