# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Substrate detection: bytes in, records out.

Deliberately separate from schema meaning. The same Postfix schema is reached
through JSONL, CSV, or a syslog file, and the substrate is what decides which
of those we are looking at — not what any of the fields mean.

Every reader is streaming and every reader is bounded by `limit`, because the
corpora this is aimed at are hundreds of megabytes and an unbounded read is a
surprise waiting for a user's CI. What was read and what was skipped is
reported rather than hidden, since a sample that is not disclosed is a
sample that gets over-read.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import syslogfmt

# Fields a plain-text reader synthesises, so downstream code has one shape to
# handle rather than a special case per substrate.
MESSAGE = "message"


@dataclass
class Substrate:
    """How to read a file, and what it turned out to be."""

    kind: str                     # jsonl | json | delimited | text | syslog
    path: Path
    delimiter: str | None = None
    fields: list[str] = field(default_factory=list)
    read: int = 0                 # records actually yielded
    available: int | None = None  # total records, when cheaply knowable
    hit_limit: bool = False       # stopped early, so there IS more in the file
    note: str = ""
    joined: int = 0               # physical lines assembled into fewer records
    skipped: int = 0              # lines that could not become a record
    # Populated for the syslog substrate only. A BSD syslog file carries no
    # year, so every timestamp it yields is a reconstruction; `facts` is how
    # that reconstruction is disclosed rather than implied.
    facts: syslogfmt.SyslogFacts | None = None

    @property
    def truncated(self) -> bool:
        """True when the scan saw less than the file holds.

        Line-oriented substrates cannot count records without reading them, so
        this is inferred from having stopped at the limit rather than measured.
        """
        if self.hit_limit:
            return True
        # `read` is zero until the file is actually consumed, so a freshly
        # detected substrate must not claim to be truncated. Making a claim
        # about coverage before reading anything is how a tool starts crying
        # truncation on every small file.
        return bool(self.available is not None and self.read
                    and self.available > self.read)

    def as_dict(self) -> dict:
        out = {
            "kind": self.kind,
            "delimiter": self.delimiter,
            "fields": list(self.fields),
            "read": self.read,
            "available": self.available,
            "truncated": self.truncated,
            "note": self.note,
            "joined_lines": self.joined,
            "skipped_lines": self.skipped,
        }
        if self.facts is not None:
            out["syslog"] = self.facts.as_dict()
        return out


def detect(path: str | Path) -> Substrate:
    """Identify the format from the extension, then confirm against the bytes.

    The extension is a hint and the first bytes are the evidence. A `.json`
    file that is actually JSON Lines is common enough in log exports that
    trusting the extension here would silently produce zero records.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no such log file: {p}")
    if p.is_dir():
        raise IsADirectoryError(
            f"{p} is a directory. Point at a file, or at a directory of them "
            f"once directory input is supported."
        )

    suffix = p.suffix.lower()
    with p.open("rb") as fh:
        head = fh.read(65536)

    if not head.strip():
        raise ValueError(f"{p} is empty")

    if suffix in {".jsonl", ".ndjson"}:
        return Substrate("jsonl", p, fields=_jsonl_fields(head))
    if suffix == ".json":
        kind = "jsonl" if _looks_like_jsonl(head) else "json"
        s = Substrate(kind, p, fields=_jsonl_fields(head) if kind == "jsonl" else [])
        if kind == "json":
            s.available = _json_array_length(head, p)
        return s
    if suffix in (".csv", ".tsv", ".psv"):
        delim = "\t" if suffix == ".tsv" else ("|" if suffix == ".psv" else ",")
        head_text = head.decode("utf-8", errors="replace")
        with contextlib.suppress(csv.Error):
            delim = csv.Sniffer().sniff(head_text[:8192],
                                        delimiters=",\t|;").delimiter
        hdr = next(csv.reader(io.StringIO(head_text), delimiter=delim), [])
        return Substrate("delimited", p, delimiter=delim, fields=[h.strip() for h in hdr])

    # Syslog has no reliable extension. `maillog` has none, and a `syslog.txt`
    # may be anything, so the bytes decide. Tried before the delimited and text
    # fallbacks because a syslog file falling through to `text` yields one
    # `message` field and no clock at all -- a schema cannot be written against
    # that, and the failure is silent.
    if suffix in (".log", ".txt", ".out", "") or suffix not in {
            ".jsonl", ".ndjson", ".json", ".csv", ".tsv", ".psv"}:
        bsd, rfc, other, _cont = syslogfmt.sample(head)
        total = bsd + rfc + other
        if bsd and rfc:
            # Two dialects in one sample is conflicting evidence, not a vote
            # with a winner. Guessing per line would assign records to the
            # wrong clock; refusing names the file for what it is instead.
            return Substrate(
                "text", p, fields=[MESSAGE],
                note=f"mixed syslog dialects in the first {total} sampled "
                     f"lines ({bsd} BSD, {rfc} RFC 5424); read as text "
                     f"rather than guessed per line")
        dialect = syslogfmt.sniff(head)
        if dialect:
            facts = syslogfmt.prepare(p, dialect)
            return Substrate(
                "syslog", p, fields=[syslogfmt.STAMP, syslogfmt.HOST,
                                     syslogfmt.IDENT, syslogfmt.PID,
                                     syslogfmt.MESSAGE],
                note=f"syslog {dialect}; the year is not in the file and is "
                     f"reconstructed from {facts.year_source}",
                facts=facts)

    # No usable extension, or an unknown one. Sniff the content.
    first = head.decode("utf-8", errors="replace").lstrip()
    if first.startswith(("{", "[")):
        return Substrate("jsonl", p, fields=_jsonl_fields(head))
    if _looks_delimited(head):
        text = head.decode("utf-8", errors="replace")
        delim = csv.Sniffer().sniff(text[:8192], delimiters=",\t|;").delimiter
        hdr = next(csv.reader(io.StringIO(text), delimiter=delim), [])
        return Substrate("delimited", p, delimiter=delim,
                         fields=[h.strip() for h in hdr])
    return Substrate("text", p, fields=[MESSAGE],
                     note="no structure detected; each line is one record "
                          "with a single 'message' field")


def _jsonl_fields(head: bytes) -> list[str]:
    for line in head.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return list(obj.keys())
    return []


def _looks_like_jsonl(head: bytes) -> bool:
    lines = [ln for ln in head.decode("utf-8", errors="replace").splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    ok = 0
    for ln in lines[:20]:
        try:
            json.loads(ln)
            ok += 1
        except json.JSONDecodeError:
            return False
    return ok >= 2


def _looks_delimited(head: bytes) -> bool:
    text = head.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.strip()][:20]
    if len(lines) < 2:
        return False
    return all(ln.count(",") >= 1 or ln.count("\t") >= 1 for ln in lines)


def _json_array_length(head: bytes, p: Path) -> int | None:
    """Length of a top-level JSON array, without reading the whole file.

    Reads a prefix looking for the closing bracket of the first element. If the
    prefix is not enough, returns None rather than reading 300MB to count.
    """
    text = head.decode("utf-8", errors="replace")
    start = text.find("[")
    if start < 0:
        return None
    # Count elements by tracking depth: every time the object nesting closes
    # back to the array's own level, one element has ended. Counting newlines
    # instead would report 1 for a file written on a single line, which is the
    # format json.dumps produces and therefore a very common one.
    depth = 0
    in_str = False
    esc = False
    count = 0
    for i in range(start + 1, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            if depth == 0:
                return count
            depth -= 1
            if depth == 0 and ch == "}":
                count += 1
    return None


def read(sub: Substrate, limit: int = 200_000) -> Iterator[dict]:
    """Yield up to `limit` records. Non-dict rows are skipped, not coerced."""
    if sub.kind == "syslog":
        facts = sub.facts or syslogfmt.prepare(sub.path, "bsd")
        sub.facts = facts
        n = 0
        for rec in syslogfmt.read(sub.path, limit, facts):
            n += 1
            yield rec
        sub.read = n
        sub.joined = facts.joined
        sub.skipped = facts.unmatched
        return
    if sub.kind == "jsonl":
        # A record is a complete JSON value, which may span lines. The old
        # reader parsed line-by-line and silently skipped every continuation
        # line of a pretty-printed object, so a file of 1,000 five-line records
        # read as 1,000 records and 4,000 silent skips. Buffer until the braces
        # balance (string-aware, so a brace inside a value does not end the
        # record), with a cap so one corrupt file cannot grow the buffer
        # without bound.
        n = 0
        hit_limit = False
        buf: list[str] = []
        depth = 0
        in_str = False
        esc = False
        started = False
        with sub.path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if n >= limit:
                    if line.strip():
                        hit_limit = True
                    break
                if not line.strip():
                    continue
                if not started:
                    if line.strip()[0] not in "{[":
                        sub.skipped += 1
                        continue
                    started = True
                buf.append(line)
                for ch in line:
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                    elif ch == '"':
                        in_str = True
                    elif ch in "{[":
                        depth += 1
                    elif ch in "]}":
                        depth -= 1
                if len(buf) > 1000 or sum(len(b) for b in buf) > 1_000_000:
                    sub.skipped += len(buf)
                    buf, depth, started = [], 0, False
                    in_str, esc = False, False
                    continue
                if started and depth <= 0 and buf:
                    try:
                        obj = json.loads("".join(buf))
                    except json.JSONDecodeError:
                        sub.skipped += len(buf)
                    else:
                        if isinstance(obj, dict):
                            n += 1
                            if len(buf) > 1:
                                sub.joined += len(buf) - 1
                            yield obj
                        else:
                            sub.skipped += len(buf)
                    buf, depth, started = [], 0, False
                    in_str, esc = False, False
            if buf:
                sub.skipped += len(buf)
        sub.read = n
        sub.hit_limit = hit_limit
    elif sub.kind == "json":
        n = 0
        with sub.path.open(encoding="utf-8", errors="replace") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for key in ("records", "rows", "data", "items", "logs"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        for obj in data if isinstance(data, list) else []:
            if n >= limit:
                break
            if isinstance(obj, dict):
                n += 1
                yield obj
        sub.read = n
    elif sub.kind == "delimited":
        n = 0
        with sub.path.open(encoding="utf-8", errors="replace", newline="") as fh:
            for row in csv.DictReader(fh, delimiter=sub.delimiter or ","):
                if n >= limit:
                    break
                clean = {k.strip(): v for k, v in row.items() if k and k.strip()}
                if clean:
                    n += 1
                    yield clean
        sub.read = n
    else:
        n = 0
        with sub.path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if n >= limit:
                    break
                line = line.rstrip("\n")
                if line.strip():
                    n += 1
                    yield {MESSAGE: line}
        sub.read = n
