# logschema — agent detail

Full reference for the build loop. The short bootstrap procedure is in
`logschema help agent-startup`; read that first.

## What this tool is

`logschema` turns a pile of log files into a **validated ingestion schema**:
which fields exist, what each one means, which one identifies a persistent
thing, which event classes occur, and how long each class is worth keeping.

It does not need a memory substrate. A schema is useful on its own, to a log
analyzer, to a CI gate, or to any consumer you have not written yet.

## What it is not

It is not the thing that writes to a brain. Ingestion is a separate act, done
afterwards, by whatever consumes the schema. One such consumer is
`positronic ingest --schema schema.yaml`.

## The build loop

| step | who | why |
|---|---|---|
| 1. obtain the base log format | **you** | vocabulary and semantics are what a language model is good at; a model knows postfix, nginx, dovecot from documentation it has read |
| 2. check it against the real data | **the tool** | this is arithmetic. It must not be the model's arithmetic, and it is where every silent error in this process has come from |
| 3. determine the real schema from 1 and 2 | **you**, using the tool's report | judgement over evidence |
| 4. write the test that validates the schema | **the tool**, plus your schema-specific expectations | the negative fixtures must be generated, not improvised |
| 5. repeat until validated | **you** drive, the tool gates | you stop when the tool says green, or when it says `exhausted` |

The loop has no CLI verb of its own, and that is deliberate. A CLI-side loop
would need a model, and then the agent in the session is doing nothing. You run
the steps; `logschema validate` decides whether the loop is finished.

## Verbs

| verb | model needed | what it does |
|---|---|---|
| `logschema help [topic]` | no | basic help, or `agent-startup` / `agent-detail` |
| `logschema doctor` | no | environment and version check |
| `logschema project init --name N --location P` | no | create the project directory and write the manifest |
| `logschema inspect PATH` | no | detect substrate, inventory fields, mine classes, and emit **proposals and questions** |
| `logschema validate PATH --schema S` | no | the six predicates; exit 0 only if all pass |
| `logschema preview PATH --schema S -n N` | no | the first N normalized events, no brain required |
| `logschema conformance write` | no | generate the conformance test and the negative fixtures |
| `logschema conformance run` | no | run it; the negative fixtures must fail |
| `logschema draft --product P` | **yes** | a candidate schema from prior knowledge — normally you write this yourself |
| `logschema revise --schema S --report R` | **yes** | a revised candidate given the failure report |
| `logschema schema` | no | the library: list, show, diff |

`logschema help` prints which of these are implemented. Do not assume.

## The questions `logschema inspect` asks

It does not answer these itself. It emits them with the evidence attached and
exits, so you can relay them to the user verbatim.

1. **Where is the data, and what produced it?** Product detection is a guess
   with a confidence, not a fact.
2. **Which field is the identity key?** Candidates are presented with the
   measurement that justifies each one, including rejections. The user picks.
3. **Which fields carry content?** All that reach the consumer's read path,
   not all that exist.
4. **What is the disposition of each class?** keep, keep-extended, demote, or
   drop.

Option 2 is the one that matters most, and the one a language model is worst
at. A model asked "which field identifies an SMTP session" has been observed
to answer "no single field does; correlate pid with the client address" — and
then, in the same answer, to reason that the daemon forks one child per
connection, which means `pid` **is** the session key. The measurement settles
it: how many distinct values, how many records each, and whether records
sharing a value are contiguous in time. A key whose groups are all singletons
is the wrong key. A key whose groups span the whole corpus is a bucket, not an
entity.

## The six predicates

A schema is valid only when all six hold. `logschema validate` is the only
thing that can report that, and it exits non-zero otherwise.

1. every field declared `identity` or `content` **is present** in the data
2. every declared class matches at least N records, and the classes cover at
   least X% of lines
3. the identity key produces groups larger than one with coherent internal
   structure — all-singletons is a failure
4. every record receives a class, or the unclassified residue is under a
   stated fraction
5. every record gets a resolvable timestamp, or the fraction without is under a
   stated fraction
6. re-running the validator produces the same verdict

Predicate 6 is there because a schema that changes when you re-run it is a
bug, and because results that are suspiciously stable have repeatedly turned
out to be measurement artefacts rather than truth.

## Class dispositions

| disposition | meaning |
|---|---|
| keep | normal retention |
| keep-extended | high value, longer retention |
| demote | keep the row, collapse content to a coarser level. No irreversible loss |
| drop | the only true delete |

Prefer demote over delete. Only genuinely redundant classes should ever be
dropped, and the test is whether the duplicates would drown the facts — not
whether they are ugly. A high duplication ratio is a fact about today's
corpus and can change tomorrow.

## Failure modes, and the fixtures that catch them

These are the negative cases `logschema conformance write` generates. Each one
must **fail**, for the stated reason. A validator that passes all of them is
detecting nothing, and "validated" then means nothing.

| fixture | must fail |
|---|---|
| identity key that is a bucket rather than an entity | predicate 3 |
| a field that does not exist in the data | predicate 1 |
| a class matching zero records | predicate 2 |
| a masked placeholder used as an identity key | predicate 1, at proposal time |
| no resolvable timestamp on any record | predicate 5 |

The masked-field case is the one that produces confidently wrong answers
rather than an error. A redacted identifier that appears as a placeholder will
be extracted as if it were a real value, so every redacted record collapses
onto one identity and the schema reports a single session for a whole corpus.
Detection has to happen when the schema is proposed, not at query time.

## When the loop cannot converge

`logschema validate` reports `exhausted` when the iteration cap is reached
with predicates still failing. That is a real result, not a failure to route
around, and it is the expected outcome for a proprietary or poorly documented
product where the model has little prior to work from.

Report it as: what was proposed, what the data contradicted, and which
predicate would have to be relaxed. Do not invent a field to make the check
pass.

## Using it as a CI gate

`logschema validate` exits zero or non-zero and prints a report, so it works as
a pre-commit or merge gate on a schema change:

```
logschema validate sample.log --schema schema.yaml
```

This is the reason the validator is a separate tool rather than a subcommand
of a brain CLI. A gate wants a binary and a report. It does not want a
substrate, an agent, or a plugin.
