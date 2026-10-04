# positronic-logschema

`logschema` builds and validates **log-ingestion schemas**: given a pile of
log files, it determines which fields exist, what each one means, which one
identifies a persistent thing, which event classes occur, and how long each
class is worth keeping.

It is a standalone tool. It does not import `memeng` or `positronic_ai`, and
it does not require either. Positronic is one *consumer* of the output
(`positronic ingest --schema schema.yaml`), not this tool's identity — which
is why the CLI describes itself by what it does rather than by which brain it
might feed.

## Install

```
uv tool install "positronic-logschema @ git+https://github.com/ShingWong/positronic-logschema.git"
```

Zero runtime dependencies, deliberately. Two reasons: the validator is the
component every downstream decision trusts, and a second implementation of it
in another language could disagree about whether a schema is valid without
anyone noticing; and this is meant to run in CI as a gate, where an install
that must resolve a dependency tree is an install that fails on someone's
machine.

## Use

```
logschema help                    # brief help, plus what is implemented
logschema help agent-startup      # bootstrap a project — read this first
logschema help agent-detail       # the build loop in full
```

Starting a project:

```
logschema project init --name mx1 --location .
logschema inspect /path/to/logs   # proposals and questions, no model needed
```

`logschema help` prints which verbs are implemented. Do not assume.

## Design commitments

**The help documents ship inside the package.** They are read by an agent at
the start of every session and then acted on, so a doc that lives in the repo
but not in the wheel is a doc the installed tool cannot read. `tests/test_help.py`
asserts that every command named in them resolves to a registered verb, and
that the manifest table in `agent-startup.md` equals what `project init`
actually writes. A help file that describes a nonexistent command sends an
agent looking for it.

**Validation is not the model's judgement.** The draft and revise steps are
the agent's, because vocabulary and semantics are what a language model is
good at. Checking a schema against real data is arithmetic, and it is where
every silent error in this process has come from, so it belongs in the tool
with an exit code.

**A loop with no designed way to stop converges on a hallucination.** The
fastest way to make a validator pass is to invent the field it wanted. So
`validate` exits non-zero unless six predicates hold, the loop reports
`exhausted` when it cannot converge, and that is a valid result rather than an
obstacle to route around.

**A validator that cannot fail is not a validator.** Every schema ships with
negative fixtures — a bucket masquerading as an identity key, a field that
does not exist, a class matching nothing, a masked placeholder used as an
identity — and all of them must fail, for the stated reason.

## Formats it can read

Detected from the bytes, never the extension — `maillog` has no extension, and
a `syslog.txt` may be JSONL.

| format | notes |
|---|---|
| JSON Lines | the common export shape |
| JSON | an array, or an object wrapping one under `records`/`rows`/`data`/`items`/`logs` |
| delimited | CSV/TSV/PSV, delimiter sniffed |
| syslog, RFC 3164 (BSD) | `Sep 27 03:27:45 mx1 postfix/smtpd[17810]: …` |
| syslog, RFC 5424 | `<134>1 2026-09-27T03:27:45Z mx1 smtpd 17810 ID47 - …` |
| plain text | one `message` field per line, and `inspect` says so |

**BSD syslog carries no year, and that is disclosed rather than papered over.**
Every timestamp from an RFC 3164 file is a reconstruction. `logschema` resolves
the year from a named source — a rotation suffix (`maillog-20260927`) in
preference to a file mtime — picks the year that puts each record nearest that
reference so that a log crossing New Year stays ordered, and reports the source
in `inspect` output along with a count of records whose year was near enough a
coin flip that it should not be relied on. A naive "wrap back a year if the
month is early" rule was tried and is wrong: it dates every record after
mid-February to the previous year. `tests/test_syslog.py` pins this down, and
the pin was checked by putting the bug back and watching the test fail.

Only the fields syslog itself delimits are extracted: `stamp`, `host`, `ident`,
`pid`, `message`. Notably absent is any program name derived from `ident` — on
a real 300,000-line Postfix log, taking the last path segment collapses
`postfix/smtp` and `postfix/amavis/smtp` into a single value. A false merge is
not recoverable downstream, so the substrate leaves that choice to the schema,
where it can be declared and argued with.

## Status

Ready and tested: `help`, `project init`, `inspect`, `validate`, `preview`,
`conformance write`, `conformance run`.

`draft` and `revise` are marked `(agent)` because the agent in the session is
the intended implementation; they are deliberately not CLI verbs, since a
CLI-side loop would need a model and would leave the agent with nothing to do.
`schema` is declared and documented but not built.

Run `logschema help` for the registry. Do not assume — a verb that is absent
from argparse prints `invalid choice`, which is a more honest answer than a stub
exiting 0.

### Evidence rather than assertion

The loop has been run end to end against a real Postfix `maillog` on a live
server. `inspect` measured it, a model drafted a schema from the report,
`validate` rejected it, and after one edit it passed:

```
VALIDATED   7/7 predicates passed
  identity_key    ident+pid measures as session: 47 events per key, median span 2m
  classes_resolve 77/77 declared classes matched an observed shape
  every_record_classed  90.2% of templatable records (threshold 90%)
```

`conformance run` then reported 6/6 negative fixtures rejected for the stated
reason, and two `validate` runs produced byte-identical output. The worked
schema and the measurements behind it are in
[`examples/postfix-maillog/`](examples/postfix-maillog/).

Two findings from that run are worth more than the schema. A plausible-looking
field present on every record can still be worthless as an identity key —
`host` held exactly one distinct value across 300,000 lines — which is why
`identity_key` is a predicate and not a comment. And a coverage number that
reaches 114% is not a rounding artefact: two classes were claiming the same
records, because containment lets a short template absorb a longer sibling.

## License

Dual-licensed AGPL-3.0-or-later / commercial, matching the rest of the project.
