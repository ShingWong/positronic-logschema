"""The help documents must not be able to describe a tool that does not exist.

These documents are read by an agent at the start of every session and then
acted on. A command name in one of them that does not resolve is not a typo,
it is a session that fails halfway with no useful error. So the names are
checked against the verb registry, and the manifest table in the startup
document is checked against the manifest the tool actually writes.

Both tests are non-vacuous: each asserts a failure when given bad input, and
that is verified by reverting the check rather than by trusting that the
assertion is reachable.
"""

from __future__ import annotations

import re

import pytest

from logschema.cli import TOP_LEVEL, VERBS, _help_text
from logschema.manifest import DIRECTORIES, MANIFEST

# Backticked spans that start a command reference, e.g. `logschema validate P`.
CMD = re.compile(r"`logschema([^`]*)`")

REGISTRY = [v for v, _, _ in VERBS]


def _resolve(words: list[str]) -> bool:
    """Greedy longest-match of a word list against the verb registry.

    `validate PATH --schema S` resolves on 'validate' and then stops at PATH,
    which is a placeholder rather than a verb. `conformance write` resolves as
    the two-word verb. `frobnicate` resolves to nothing, which is the failure.
    """
    for i in range(len(words), 0, -1):
        if " ".join(words[:i]) in REGISTRY:
            return True
    return False


def _commands(doc: str) -> list[list[str]]:
    out = []
    for m in CMD.finditer(doc):
        words = re.findall(r"[a-z][a-z0-9-]*", m.group(1))
        if words:
            out.append(words)
    return out


@pytest.mark.parametrize("topic", ["agent-startup", "agent-detail"])
def test_help_document_exists_and_is_not_empty(topic):
    text = _help_text(f"{topic}.md")
    assert len(text) > 400, f"{topic} is too short to be a usable procedure"


@pytest.mark.parametrize("topic", ["agent-startup", "agent-detail"])
def test_every_command_in_help_resolves(topic):
    """A command named in a help document must exist in the registry."""
    cmds = _commands(_help_text(f"{topic}.md"))
    assert cmds, "no command references found — the extractor is broken, " \
                  "not the document"
    bad = [c for c in cmds if not _resolve(c)]
    assert not bad, (
        f"{topic} names commands that do not resolve to a verb: "
        f"{[' '.join(c) for c in bad]}. Either add the verb to VERBS or fix "
        f"the document. A help file that describes a nonexistent command "
        f"sends the agent looking for it."
    )


def test_extractor_catches_a_command_that_does_not_exist():
    """Non-vacuity for the check above: the resolver must reject nonsense."""
    assert _resolve(["validate", "PATH"]) is True
    assert _resolve(["conformance", "write"]) is True
    assert _resolve(["frobnicate"]) is False
    assert _resolve(["project", "init"]) is True
    # Right words, wrong pairing: must not pass as either verb.
    assert _resolve(["conformance", "init"]) is False


def test_top_level_verbs_are_all_reachable():
    for verb in TOP_LEVEL:
        assert any(v == verb or v.startswith(verb + " ") for v in REGISTRY), (
            f"{verb} is advertised as a top-level verb but no registry entry "
            f"starts with it"
        )


def test_manifest_table_in_startup_matches_the_manifest():
    """The document shows the manifest; the tool writes the manifest.

    They are the same list, asserted equal. Otherwise a user reads a file list
    that `project init` does not produce.
    """
    doc = _help_text("agent-startup.md")
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*(.+?)\s*\|$", doc, re.MULTILINE)
    assert rows, "no manifest table found in agent-startup.md — if the table " \
                 "was reformatted, fix the extractor here too"
    from_doc = [(p, purpose) for p, purpose in rows]
    assert from_doc == MANIFEST, (
        "agent-startup.md and manifest.py disagree about what a project "
        f"contains.\n  doc: {[p for p, _ in from_doc]}\n  code: "
        f"{[p for p, _ in MANIFEST]}"
    )


def test_every_manifest_entry_has_a_purpose():
    for path, purpose in MANIFEST:
        assert purpose.strip(), f"{path} has no stated purpose"
        assert len(purpose) > 20, (
            f"{path} purpose is too terse to be useful in a scaffolded file: "
            f"{purpose!r}"
        )


def test_directories_are_a_subset_of_the_manifest():
    for d in DIRECTORIES:
        assert any(p == d for p, _ in MANIFEST), (
            f"{d} is treated as a directory but is not in the manifest"
        )


def test_ready_verbs_are_the_ones_the_cli_actually_registers():
    """`ready` is a claim. Hold it to the parser."""
    from logschema.cli import build_parser

    parser = build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    registered = set()
    for a in actions:
        registered |= set(a.choices or {})

    for verb, status, _ in VERBS:
        head = verb.split()[0]
        if status == "ready":
            assert head in registered, (
                f"{verb} is marked ready but `{head}` is not a registered "
                f"subcommand of the CLI"
            )
        else:
            assert head not in registered or head in {
                v.split()[0] for v, st, _ in VERBS if st == "ready"
            }, f"{verb} is not ready but `{head}` is already registered"
