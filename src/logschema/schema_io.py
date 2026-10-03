# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Emit a schema as YAML. The inverse of `miniyaml`.

Round-tripping matters for two reasons. `conformance write` emits mutated
schemas a human has to be able to read, because a fixture nobody believes is a
fixture nobody maintains. And a schema author who edits by hand needs the
output to keep their comments and ordering rather than being re-serialised into
something they have to re-read from scratch.

Anything the reader supports has to be writable, or the loop's output cannot be
the loop's input.
"""

from __future__ import annotations

import re

_PLAIN = re.compile(r"^[A-Za-z0-9_./+@][A-Za-z0-9 _./+@:=-]*$")


def _scalar(v) -> str:
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s and _PLAIN.match(s) and not s.endswith(":") and ": " not in s:
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_yaml(obj, indent: int = 0) -> str:
    """Render nested maps/lists/scalars. Maps and lists of maps go block style;
    a list of plain scalars goes flow, because one item per line for three
    strings is noise."""
    pad = " " * indent
    if isinstance(obj, dict):
        if not obj:
            return pad + "{}\n"
        out = []
        for k, v in obj.items():
            key = _scalar(k)
            if isinstance(v, dict) and v:
                out.append(f"{pad}{key}:\n{dump_yaml(v, indent + 2)}")
            elif isinstance(v, list) and v:
                if all(not isinstance(x, (dict, list)) for x in v):
                    out.append(f"{pad}{key}: [{', '.join(_scalar(x) for x in v)}]\n")
                else:
                    out.append(f"{pad}{key}:\n{dump_yaml(v, indent + 2)}")
            elif isinstance(v, (dict, list)):
                out.append(f"{pad}{key}: {'{}' if isinstance(v, dict) else '[]'}\n")
            else:
                out.append(f"{pad}{key}: {_scalar(v)}\n")
        return "".join(out)
    if isinstance(obj, list):
        if not obj:
            return pad + "[]\n"
        out = []
        for item in obj:
            if isinstance(item, dict) and item:
                body = dump_yaml(item, indent + 2)
                first, _, rest = body.partition("\n")
                out.append(f"{pad}- {first.strip()}\n")
                if rest.strip():
                    out.append(rest if rest.endswith("\n") else rest + "\n")
            elif isinstance(item, list) and item:
                out.append(f"{pad}-\n{dump_yaml(item, indent + 2)}")
            else:
                out.append(f"{pad}- {_scalar(item)}\n")
        return "".join(out)
    return pad + _scalar(obj) + "\n"
