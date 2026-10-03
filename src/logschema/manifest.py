# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""The project manifest: what `logschema project init` writes.

Single source of truth. `tests/test_help.py` asserts that the table in
`help/agent-startup.md` matches this list exactly, so the document a user
reads and the files the tool creates cannot drift apart.
"""

from __future__ import annotations

# (path relative to project root, one-line purpose)
MANIFEST: list[tuple[str, str]] = [
    (
        "README.md",
        (
            "What this project is, the software, the data source, and how to "
            "re-run every step."
        ),
    ),
    (
        "SCHEMA.md",
        (
            "Fields with roles and meanings, event classes with meanings, the "
            "identity key, and the evidence that chose it."
        ),
    ),
    (
        "FINDINGS.md",
        (
            "The class inventory: frequency, chain participation, identity "
            "resolution, content mass, and the proposed disposition per class."
        ),
    ),
    (
        "DECISIONS.md",
        (
            "Every choice the user made, with its reason and the date. Exists "
            "so the next person does not re-derive them."
        ),
    ),
    (
        "VALIDATION.md",
        (
            "What was checked, what failed, and the iteration history of the "
            "build loop."
        ),
    ),
    (
        "schema.yaml",
        (
            "The machine artifact: fields, roles, types, classes, "
            "dispositions."
        ),
    ),
    (
        "conformance/",
        (
            "The conformance test, plus the negative fixtures that MUST fail."
        ),
    ),
]

# Manifest entries that are directories rather than files.
DIRECTORIES = {"conformance/"}
