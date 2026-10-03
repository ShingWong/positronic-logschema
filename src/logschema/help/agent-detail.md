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
| `logschema validate PATH --schema S` | no | every predicate; exit 0 only if all pass |
| `logschema preview PATH --schema S -n N` | no | the first N normalized events, no brain required |
| `logschema conformance write PATH --schema S --out D` | no | generate the negative fixtures |
| `logschema conformance run PATH --fixtures D` | no | run them; every one must be rejected |
| `logschema draft --product P` | **yes** | a candidate schema from prior knowledge — normally you write this yourself |
| `logschema revise --schema S --report R` | **yes** | a revised candidate given the failure report |
| `logschema schema` | no | the library: list, show, diff |
| `logschema preview PATH --schema S -n N` | no | see what ingestion would produce |

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

## The predicates

A schema is valid only when all of these hold. `logschema validate` is the
only thing that can report that, and it exits non-zero otherwise.

| # | predicate | fails when |
|---|---|---|
| 1 | `fields_present` | a declared field is not in the data |
| 1b | `declared_fields_unmasked` | a field declared `identity` is almost entirely redaction placeholders |
| 3 | `identity_key` | the claimed key measures as `bucket`, `noise`, or `undetermined` |
| 2 | `classes_resolve` | a declared class corresponds to no observed shape |
| 4 | `every_record_classed` | coverage of templatable records is under 90% |
| 5 | `clock_resolvable` | no field parses as a timestamp, or the declared one is not the best |
| 6 | `deterministic` | the verdict is not a pure function of schema and data |

Three of these deserve their reasoning stated:

**`bucket` is rejected as an identity key.** A schema whose key is a category
passes every field-presence check and looks entirely reasonable. It is wrong,
and nothing but measurement catches it. `svc` with 800 events per value spread
across 95% of the timeline denotes a service, not a thing.

**A partial scan fails `identity_key` outright** rather than passing
provisionally. A window cannot show that a key recurs; a schema validated on a
window is a schema that is wrong on the file.

**Redaction is judged per role, not per field.** A redacted `identity` field is
fatal — every record collapses onto the placeholders. A redacted `content`
field is fine, because the template survives redaction and that text is what
class shapes are mined from. A real mail log is ~87% placeholders in its
message text; failing that would make the predicate useless.

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

## `preview`: what comes out the other end

`validate` says the schema is self-consistent with the data. `preview` shows
what a consumer would actually receive, and it writes nothing.

```
logschema preview PATH --schema S -n 20
```

Three things it guarantees, each of which exists because the opposite reports
wrong data confidently rather than failing:

- **A redacted value is never passed through as a value.** It becomes `null`
  with `state: redacted` and the placeholder recorded separately. `<HEX>` is
  not an address, and a consumer that groups by a field value must not be able
  to read one as the other.
- **Provenance travels with every field** — `exact`, `redacted`, `absent`,
  `empty`, or `unresolved`. A timestamp that fell back to arrival time is not
  the same fact as one read from the record.
- **Identity is the schema's claim, checked, not re-guessed.** `validate`
  already decided; the projection records what the key resolved to.

Note the asymmetry, which is deliberate: `subject_norm` and `body_text` carry
the raw text with its redaction markers intact, because the surviving shape is
what classes are mined from. Structured `fields` are nulled. Both are reported.

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
