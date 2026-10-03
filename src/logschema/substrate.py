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

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

# Fields a plain-text reader synthesises, so downstream code has one shape to
# handle rather than a special case per substrate.
MESSAGE = "message"


@dataclass
class Substrate:
    """How to read a file, and what it turned out to be."""

    kind: str                     # jsonl | json | delimited | text
    path: Path
    delimiter: str | None = None
    fields: list[str] = field(default_factory=list)
    read: int = 0                 # records actually yielded
    available: int | None = None  # total records, when cheaply knowable
    hit_limit: bool = False       # stopped early, so there IS more in the file
    note: str = ""

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
        return {
            "kind": self.kind,
            "delimiter": self.delimiter,
            "fields": list(self.fields),
            "read": self.read,
            "available": self.available,
            "truncated": self.truncated,
            "note": self.note,
        }


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
        try:
            delim = csv.Sniffer().sniff(head_text[:8192], delimiters=",\t|;").delimiter
        except csv.Error:
            pass
        hdr = next(csv.reader(io.StringIO(head_text), delimiter=delim), [])
        return Substrate("delimited", p, delimiter=delim, fields=[h.strip() for h in hdr])

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
    if sub.kind == "jsonl":
        n = 0
        hit_limit = False
        with sub.path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if n >= limit:
                    # Peek one more line so `hit_limit` distinguishes "the file
                    # ended exactly at the limit" from "there was more". A file
                    # with precisely `limit` records is not truncated, and
                    # claiming otherwise on every exact-size file would make the
                    # disclosure noise.
                    if line.strip():
                        hit_limit = True
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    n += 1
                    yield obj
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
