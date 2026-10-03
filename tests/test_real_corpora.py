"""Run `inspect` against the real corpora and check it against what we know.

These assertions are not snapshots of this tool's output. Each one states a
fact established by independent measurement earlier in the project, so the test
fails if `inspect` starts disagreeing with the data rather than merely changing.

Skipped when a corpus is absent, so the suite still runs on a fresh checkout.
"""

from __future__ import annotations

import os

import pytest

from logschema.inspect_cmd import inspect_path

CORPORA = "/tmp/opencode"


def have(name: str) -> bool:
    return os.path.exists(os.path.join(CORPORA, name))


pytestmark = pytest.mark.skipif(
    not any(have(f) for f in ("mx1_maillog.jsonl", "pvl_merged.jsonl",
                              "git_subjects.jsonl")),
    reason="corpora not present",
)


@pytest.mark.skipif(not have("mx1_maillog.jsonl"), reason="mx1 absent")
def test_mx1_finds_the_known_clock_and_structure():
    r = inspect_path(f"{CORPORA}/mx1_maillog.jsonl", limit=60_000)

    # `stamp` is a real ISO timestamp; `pid` and `ident` are not. Ranked by
    # behaviour, so the answer must be `stamp` regardless of what it is called.
    assert r["time_candidates"][0]["field"] == "stamp"
    assert r["time_candidates"][0]["monotonic"] > 0.99

    names = {f["name"] for f in r["fields"]}
    assert {"svc", "pid", "ident", "stamp", "tok"} <= names

    # `svc` has 15 values over 1.5M lines and its events span the whole corpus:
    # a category. `pid` groups 132 lines into minutes-long bursts: a session.
    # This is the pair of judgements the whole tool exists to make, and it is
    # the case a language model gets wrong by hedging.
    #
    # Sampled at 60k of 1.5M lines, `pid` is measured on the pids visible in
    # that window. They still cluster — the file is time-ordered, so a prefix
    # holds whole sessions rather than scattering them.
    by = {tuple(c["fields"]): c for c in r["identity_candidates"]}
    assert by[("svc",)]["verdict"] == "bucket", by[("svc",)]["evidence"]
    assert by[("pid",)]["verdict"] == "session", by[("pid",)]["evidence"]
    assert by[("pid",)]["mean_size"] > 2

    # mx1 is protocol text and templates cleanly.
    cl = r["classes"]
    assert cl["text_field"] == "tok"
    assert cl["untemplatable_share"] < 0.5, (
        f"{cl['untemplatable_share']:.0%} untemplatable — protocol text should "
        f"reduce to shapes")
    assert cl["distinct"] > 100


@pytest.mark.skipif(not have("mx1_maillog.jsonl"), reason="mx1 absent")
def test_mx1_redaction_is_surfaced_not_used_as_a_key():
    r = inspect_path(f"{CORPORA}/mx1_maillog.jsonl", limit=30_000)
    tok = next(f for f in r["fields"] if f["name"] == "tok")
    assert tok["placeholder_rate"] > 0.5, "masking not detected in tok"
    q = {x["id"] for x in r["questions"]}
    assert "redaction" in q, "redaction question not raised"


@pytest.mark.skipif(not have("pvl_merged.jsonl"), reason="pvl absent")
def test_pvl_is_annotations_not_a_template_corpus():
    # Explicitly above the row count (331,147). The default 200,000 limit is
    # itself a prefix of this corpus, and the tool correctly says so — which is
    # why this test has to ask for a full scan to assert a real verdict.
    r = inspect_path(f"{CORPORA}/pvl_merged.jsonl", limit=400_000)
    assert r["coverage"]["is_prefix_of_timeline"] is False
    assert r["coverage"]["identity_verdicts_are"] == "measured on the full corpus"

    # `desc` is 91.8% empty. It must not be proposed as the content field, and
    # the sparseness must be disclosed.
    sparse = {s["field"]: s["fill_rate"] for s in r["data_health"]["sparse_fields"]}
    assert sparse.get("desc", 1.0) < 0.2, "desc sparsity not disclosed"

    # `who` is the visitor id: ~100k values over 331k rows, 3.3 visits each.
    # An entity, not a session and emphatically not a bucket. The whole file
    # must be scanned for this to be measurable — see the prefix test below.
    by = {tuple(c["fields"]): c for c in r["identity_candidates"]}
    assert by[("who",)]["verdict"] == "entity", by[("who",)]["evidence"]
    assert by[("who",)]["groups"] > 5_000
    assert 2.5 < by[("who",)]["mean_size"] < 4.5

    # The top two "shapes" in this corpus were free-text tour names. The class
    # inventory must say so rather than presenting them as templates.
    q = next(x for x in r["questions"] if x["id"] == "classes")
    assert q["evidence"]["untemplatable_share"] > 0.5 or \
        "free-form" in q["note"]


@pytest.mark.skipif(not have("pvl_merged.jsonl"), reason="pvl absent")
def test_a_prefixed_scan_refuses_to_call_a_real_entity_noise():
    """The defect this test exists to prevent.

    pvl's first 60,000 rows contain almost nobody's second visit: 58,787
    distinct visitors at 1.02 events each. A tool that reported that as `noise`
    would tell the user their corpus has no entity field, when over the whole
    file `who` is 99,987 visitors at 3.31 events each. The data did not change;
    the window did. So a partial scan must yield `undetermined`, and must say
    that identity verdicts are indicative only.
    """
    part = inspect_path(f"{CORPORA}/pvl_merged.jsonl", limit=60_000)
    whole = inspect_path(f"{CORPORA}/pvl_merged.jsonl", limit=400_000)

    assert part["coverage"]["is_prefix_of_timeline"] is True
    assert "indicative only" == part["coverage"]["identity_verdicts_are"]
    assert part["coverage"]["explanation"]

    p_who = next(c for c in part["identity_candidates"] if c["fields"] == ["who"])
    w_who = next(c for c in whole["identity_candidates"] if c["fields"] == ["who"])

    assert p_who["verdict"] == "undetermined", (
        "a prefix of the timeline cannot support a negative identity verdict; "
        f"got {p_who['verdict']} — {p_who['evidence']}")
    assert "prefix of the timeline" in p_who["evidence"]
    assert w_who["verdict"] == "entity", w_who["evidence"]

    q = next(x for x in part["questions"] if x["id"] == "identity")
    assert "Rescan without --limit" in q["note"], (
        "the user must be told the verdicts need a full scan")
    assert q["evidence"]["partial_coverage"] is True

    # And the plain-text rendering must not bury it.
    from logschema.inspect_cmd import render
    out = render(part)
    assert "PREFIX OF THE TIMELINE" in out


@pytest.mark.skipif(not have("git_subjects.jsonl"), reason="git absent")
def test_git_two_fields_still_produces_a_usable_report():
    r = inspect_path(f"{CORPORA}/git_subjects.jsonl", limit=50_000)
    names = {f["name"] for f in r["fields"]}
    assert names == {"ts", "subj"}
    # Bare epoch seconds.
    assert r["time_candidates"][0]["strategy"] == "epoch"
    # Commit subjects are prose, so most records must be reported untemplatable.
    assert r["classes"]["untemplatable_share"] > 0.5
    # A two-field corpus still gets a full question set, including the clock.
    ids = {q["id"] for q in r["questions"]}
    assert {"data", "clock", "content", "classes"} <= ids
