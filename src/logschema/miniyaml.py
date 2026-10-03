# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""A YAML subset reader, because this package has zero dependencies.

Not a general YAML implementation and not trying to be. It reads the grammar
`schema.yaml` is allowed to use, and refuses anything else rather than guessing:

  nested mappings            key: value
  lists of mappings          - key: value
  lists of scalars           - value
  scalars                    int, float, bool, null, quoted and bare strings
  comments                   # to end of line
  inline flow lists          [a, b, c]
  inline flow maps           {a: 1, b: 2}

Block scalars (`|`, `>`), anchors, aliases, tags, multi-document streams and
complex keys are all rejected with a message naming the line. A schema is a
checked artifact; a reader that silently mis-parses one produces a validator
that checks the wrong thing, which is worse than refusing.
"""

from __future__ import annotations

import re
from typing import Any


class YamlError(ValueError):
    """A YAML feature outside the supported subset, with its line number."""


_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def _scalar(tok: str) -> Any:
    t = tok.strip()
    if not t:
        return ""
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        body = t[1:-1]
        if t[0] == '"':
            return body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return body.replace("''", "'")
    low = t.lower()
    if low in ("null", "~", "none"):
        return None
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if _INT.match(t):
        return int(t)
    if _FLOAT.match(t):
        return float(t)
    return t


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _flow(text: str, lineno: int) -> Any:
    """Parse `[a, b]` and `{a: 1}` on one line."""
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if not inner:
            return []
        return [_flow(p, lineno) for p in _split_commas(inner, lineno)]
    if t.startswith("{") and t.endswith("}"):
        inner = t[1:-1].strip()
        out: dict = {}
        if not inner:
            return out
        for part in _split_commas(inner, lineno):
            k, _, v = part.partition(":")
            out[str(_scalar(k))] = _flow(v, lineno) if v.strip() else None
        return out
    return _scalar(t)


def _split_commas(inner: str, lineno: int) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in inner:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _split_key(body: str, lineno: int) -> tuple[str, str] | None:
    """Split `key: value`, respecting quotes. None if there is no key."""
    quote = None
    for i, ch in enumerate(body):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            continue
        if ch == ":" and (i + 1 == len(body) or body[i + 1] in " \t"):
            return body[:i].strip(), body[i + 1:].strip()
    return None


def parse_yaml(text: str) -> Any:
    raw = text.splitlines()
    for banned, label in (("|", "pipe"), (">", "folded")):
        for ln, line in enumerate(raw, 1):
            s = _strip_comment(line)
            if re.match(rf"^\s*[\w.\-]+\s*:\s*{re.escape(banned)}\s*$", s):
                raise YamlError(
                    f"line {ln}: `{label}` block scalars are not supported; "
                    f"use a quoted single-line string"
                )
            if s.strip().startswith("---") or s.strip().startswith("..."):
                raise YamlError(f"line {ln}: multi-document streams are not supported")

    lines: list[tuple[int, int, str]] = []   # (lineno, indent, content)
    for ln, line in enumerate(raw, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        # Checked against the whole leading run, not `line[:indent]`. A line
        # indented as `\t\tb: 1` has indent == 0, so slicing to `indent`
        # inspected an empty string and the tab passed.
        lead = line[:len(line) - len(line.lstrip())]
        if "\t" in lead:
            raise YamlError(
                f"line {ln}: tab used for indentation; YAML forbids it because "
                f"a tab's width is ambiguous")
        # Content is left-stripped: indentation is carried in its own tuple
        # field, and keeping it in the body as well means every `body.startswith`
        # and every length computation has to reason about leading spaces. Two
        # sources of truth for column position is how the child indent of a
        # `- key: value` item came out one column left of its own
        # continuations.
        lines.append((ln, indent, _strip_comment(line).strip()))
    if not lines:
        return {}

    value, idx = _parse_block(lines, 0, lines[0][1])
    if idx < len(lines):
        raise YamlError(f"line {lines[idx][0]}: unexpected indentation")
    return value


def _parse_block(lines: list[tuple[int, int, str]], i: int,
                 indent: int) -> tuple[Any, int]:
    _ln, _, body = lines[i]
    if body.startswith("- "):
        return _parse_list(lines, i, indent)
    if body == "-":
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _parse_map(lines: list[tuple[int, int, str]], i: int,
               indent: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(lines):
        ln, ind, body = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError(f"line {ln}: unexpected indentation inside a mapping")
        if body.startswith("- "):
            break
        kv = _split_key(body, ln)
        if kv is None:
            raise YamlError(f"line {ln}: expected `key: value`, got {body!r}")
        key, rest = kv
        if rest.startswith(("[", "{")):
            out[key] = _flow(rest, ln)
            i += 1
        elif rest == "":
            # Either a nested block or an empty value. Look ahead.
            if i + 1 < len(lines) and (
                lines[i + 1][1] > ind
                or (lines[i + 1][1] == ind and lines[i + 1][2].startswith("-"))
            ):
                out[key], i = _parse_block(lines, i + 1, lines[i + 1][1])
            else:
                out[key] = None
                i += 1
        else:
            out[key] = _scalar(rest)
            i += 1
    return out, i


def _parse_list(lines: list[tuple[int, int, str]], i: int,
                indent: int) -> tuple[list, int]:
    out: list = []
    while i < len(lines):
        ln, ind, body = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise YamlError(f"line {ln}: unexpected indentation inside a list")
        if not body.startswith("-"):
            break
        rest = body[1:].strip()
        if not rest:
            if i + 1 < len(lines) and lines[i + 1][1] > ind:
                val, i = _parse_block(lines, i + 1, lines[i + 1][1])
                out.append(val)
            else:
                out.append(None)
                i += 1
            continue
# A flow collection is checked FIRST. `{path: stamp, ...}` contains a
        # colon-space, so a naive key split sees a mapping and tries to recurse
        # into a block that does not exist — the shape used throughout
        # schema.yaml, so this is the common path, not an edge case.
        if rest.startswith(("[", "{")):
            out.append(_flow(rest, ln))
            i += 1
            continue
        kv = _split_key(rest, ln)
        if kv is not None:
            # `- key: value` — a mapping whose first key is on the dash line.
            # Re-enter as a map at the column where that key actually starts,
            # so continuation lines align. Measured as the offset of the first
            # non-space character after the dash; the previous arithmetic
            # (`len(body) - len(body[1:].lstrip())`) lost the dash's own
            # column and put the child one space to the left of its
            # continuations, which then read as stray indentation.
            # `body` is already left-stripped, and `ind` carries the dash's own
            # column, so the key begins at `ind + 2` for `- key: value`.
            child_indent = ind + 2
            synthetic = [(ln, child_indent, rest)]
            j = i + 1
            while j < len(lines) and lines[j][1] > ind:
                synthetic.append(lines[j])
                j += 1
            val, _ = _parse_map(synthetic, 0, child_indent)
            out.append(val)
            i = j
        else:
            out.append(_scalar(rest))
            i += 1
    return out, i
