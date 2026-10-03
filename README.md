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

## Status

`help` and `project init` are implemented and tested. `inspect`, `validate`,
`preview`, `conformance` and `schema` are declared in the verb registry and
documented, but not yet built. `draft` and `revise` are marked `(agent)`
because the agent in the session is the intended implementation; they are
deliberately not CLI verbs, since a CLI-side loop would need a model and
would leave the agent with nothing to do.

The build loop has been exercised by hand against Postfix `maillog`
(1,499,352 lines, 15 daemons) and the findings are in
`../positronic-logschema/RESEARCH.md`.

## License

Dual-licensed AGPL-3.0-or-later / commercial, matching the rest of the project.
