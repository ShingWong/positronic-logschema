# =====================================================================
# Project Positronic
# Copyright (C) 2026 Shing Wong. All Rights Reserved.
# =====================================================================

"""Build and validate log-ingestion schemas.

This package is deliberately standalone. It does not import `memeng` or
`positronic_ai`, and it does not require either. Steps 1 through 5 of the
build loop -- draft, check against real data, revise, write the conformance
test, repeat until validated -- are useful with no memory substrate present,
which is what makes this usable as a CI gate on a schema change.

Positronic is one *consumer* of the output (`positronic ingest --schema`),
not the tool's identity. The CLI describes itself by what it does so that a
user on an unrelated harness, with no brain at all, can still use it.
"""

__version__ = "0.1.0"
