# postfix.maillog — a worked example

The schema in this directory was produced by running the loop end to end
against a real 1,499,352-line Postfix `maillog`, and it validates:

```
$ logschema validate maillog.jsonl --schema schema.yaml
  VALIDATED   7/7 predicates passed
    identity_key    svc+pid measures as session: 61 events per key inside a
                    median 2 minutes — 0.0% of the corpus timeline
    classes_resolve 100/100 declared classes matched an observed shape
    every_record_classed  92.2% of templatable records (threshold 90%)
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
   matching nothing.
4. One line was removed and it passed.

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
classes are needed to reach 90% coverage; the model declared 101.
