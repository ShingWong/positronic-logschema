# AGENTS.md — positronic-logschema

Python package `logschema`. Builds and validates **log-ingestion schemas** for
memory substrates and log analyzers. Standalone: it does not import `memeng`
or `positronic_ai`, and must never grow a dependency on either. That
constraint is what makes it usable as a CI gate and by users on an unrelated
harness with no brain installed.

## Layout

```
src/logschema/
  cli.py          verb registry (VERBS) + argparse. The registry is the
                  allow-list the help-drift test checks against.
  manifest.py     what `project init` writes. Single source of truth; the
                  startup doc's table is asserted equal to it.
  help/
    agent-startup.md   bootstrap procedure. Short. Read by the agent first.
    agent-detail.md    the build loop in full.
tests/test_help.py      drift tests — see below.
```

## The two rules that matter

**The help documents are the product.** An agent reads them at the start of a
session and acts on them. So they ship as package data (`[tool.setuptools.package-data]`),
and `tests/test_help.py` asserts that every `logschema ...` reference in them
resolves against `VERBS`, and that the manifest table matches `manifest.py`.
If you add a verb, add it to `VERBS`; if you rename one, the tests will tell
you which document still mentions the old name.

**Validation is not the model's judgement.** `draft` and `revise` are the
agent's steps and are marked `(agent)` in the registry — they are deliberately
not CLI verbs, because a CLI-side loop would need a model and would leave the
agent in the session with nothing to do. Everything that checks a claim
against data is a CLI verb with an exit code, and it must never accept a claim
because the model asserted it.

## Status vocabulary

`VERBS` carries a status per verb, and `logschema help` prints it:

- `ready` — registered in argparse and working. The test asserts this claim
  against the actual parser, so it cannot rot.
- `planned` — declared and documented, not built. **Deliberately not
  registered in argparse.** Do not add a stub that prints "not yet
  implemented" and exits 0: an agent reads that as success, which is worse
  than argparse's "invalid choice" listing what does exist.
- `agent` — the agent implements it, guided by the help docs.

## Testing discipline

The drift tests are non-vacuous and were verified by breaking each check and
watching it fail: renaming a verb in a document, and desynchronising the
manifest table from `manifest.py`.

When adding a validator predicate, add a negative fixture with it. A
validator that passes a deliberately-wrong schema detects nothing, and
"validated" then has no referent. The fixtures currently specified: identity
key that is a bucket, a field that does not exist, a class matching zero
records, a masked placeholder used as an identity key, and no resolvable
timestamp.

## Do not

- add a dependency on positronic, memeng, or any agent SDK
- make the tool block on stdin without `--interactive`; a harness that does
  not wire stdin must get a question and an exit code, never a hang
- rely on ANSI colour or TTY width for anything load-bearing
- register a planned verb to make a document resolve
