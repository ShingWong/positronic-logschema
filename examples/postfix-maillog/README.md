# postfix.maillog — a worked example

The schema in this directory was produced by running the loop end to end
against a real 1,499,352-line Postfix `maillog`, and it validates:

```
$ logschema validate maillog.jsonl --schema schema.yaml
  VALIDATED   7/7 predicates passed
    identity_key    svc+pid measures as session: 61 events per key inside a
                    median 2 minutes — 0.0% of the corpus timeline
    classes_resolve 77/77 declared classes matched an observed shape
    every_record_classed  90.2% of templatable records (threshold 90%)
  exit 0
```

and every negative fixture built from it is rejected:

```
$ logschema conformance run maillog.jsonl --fixtures ./fixtures
  6/6 negative fixtures rejected for the stated reason
  exit 0
```

## How it was made

1. `logschema inspect` measured the file. The corpus has 5,233 distinct event
   shapes over 984,278 templatable records, with a median shape frequency of
   **1** and 2,903 shapes seen exactly once.
2. A model drafted the schema from that measurement — 101 classes, `[svc, pid]`
   as the identity key, `stamp` as the clock.
3. `logschema validate` rejected the first draft on one predicate: a class
   matching nothing. One line came out and it passed.
4. 23 classes were then removed because their templates named real peer
   hostnames the redaction pass had missed — see below.

## Two things to know before copying this

**It is deliberately generic, and coverage is correspondingly tight.** The
draft had 101 classes; 23 of them existed only because a client hostname
survived masking — the same `connect from` line, written once with the host
intact and once with it masked. Those are the same event, so they were redundant
with the generic shape, and removing them cost 2.0 points of coverage — 92.2% to
**90.2% against a 90% threshold**. That is 0.2 points of headroom, and it will
not survive a corpus with a different shape mix.

If you have unmasked logs, add the specific forms back. If you do not, either
accept the residue deliberately or raise the threshold knowing you are choosing
to. Do not add classes you have not seen in your own data.

**Peer hostnames are why this file is shorter than the first draft.** The
redaction pass replaced IPs, emails and hex ids but left some peer hostnames and
the local mail domain intact, so those templates would have shipped real network
identifiers into a public repository. They were removed, not masked, because the
generic class already covers them. Run the same check before publishing a schema
of your own:

```
grep -nE '<your-domain>|<peer-hostname>' schema.yaml
```

## What the measurement decided

| decision | value | why |
|---|---|---|
| identity key | `[svc, pid]` | 24,537 groups at 61.1 events each, median span 2 minutes. A bare `pid` measures well too but collides across daemons |
| `svc` alone | rejected | 15 values, 99,957 events each, spread over 26 days — a category |
| clock | `stamp` | parses 100%, monotonic 100% |
| content | `tok` | 91% redaction placeholders, and that is fine: the shape survives |

`svc` is the case worth reading twice. It is a plausible-looking field name, it
is present on every record, and it is useless as an identity key. Nothing but
measurement catches that, which is why `identity_key` is a predicate and not a
comment.

## The masker's residue

`connect from` appears 97,043 times and `connect from us` 8,137 times. They are
the same log line — the second is the first with a host that survived masking.
One class covers both, because template matching is ordered containment. 91
classes are needed to reach 90% coverage, and this schema declares 77 — fewer
than the measurement says are needed, because containment lets a short shape
absorb its longer siblings.
