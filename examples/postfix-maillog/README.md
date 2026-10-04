# postfix.maillog — a worked example

The schema here was produced by running the loop end to end against a **raw,
unmasked RFC 3164 syslog file** read from a live mail server — 300,000 lines,
20 program tags, 6,738 distinct event shapes.

```
$ logschema validate /var/log/maillog --schema schema.yaml
  VALIDATED   7/7 predicates passed
    fields_present       5 declared, 0 missing
    identity_key         ident+pid measures as session: 47 events per key
                         inside a median 2 minutes — 0.1% of the corpus timeline
    classes_resolve      73/73 declared classes matched an observed shape
    every_record_classed 97.7% of templatable records (threshold 90%)
    clock_resolvable     stamp parses 100% of records and is monotonic 100%
  exit 0
```

Two runs produced byte-identical JSON (18,525 bytes).

## Why this file exists, and what it cost to get right

The first version of this example was validated against a JSONL file that an
unshipped ad-hoc script had produced from the same log. Pointed at the real
syslog file it scored **3 of 7 predicates**: four of its five field names did
not exist, all 77 classes matched nothing, coverage 0.0%.

The cause is worth stating because it is easy to repeat. The JSONL had been
given fields the raw format does not have — a derived program name, a queue-id
list, a file name, a renamed message field. A schema written against a
transformed copy is a schema written against a screenshot. Nothing catches it
except running it against the file it is for, which is why `substrate` now
reads syslog directly and why this example is measured on the raw bytes.

## What the measurement decided

| decision | value | why |
|---|---|---|
| identity key | `[ident, pid]` | 6,367 groups, 47.1 events each, median span 2 minutes |
| clock | `stamp` | parses 100%, monotonic 100% |
| content | `message` | carries the queue id, which is the real message identity |
| `host` | declared, never a key | **one** distinct value across 300,000 lines |
| `ident` | declared verbatim | last-path-segment derivation merges `postfix/smtp` with `postfix/amavis/smtp` |

`host` is the case worth reading twice. It is present on every record, it has a
plausible name, and it is worthless — a single value. Nothing but measurement
catches that, which is why `identity_key` is a predicate and not a comment.

## The loop, and what it removed

A model drafted 83 classes from the `inspect` report. `validate` rejected ten of
them for matching nothing — they had been written from prior knowledge of
Postfix rather than copied from the measured vocabulary. That is the failure the
loop exists to catch, and it caught ten in one pass. After removing them:

73 classes, 97.7% coverage. The measurement said 206 classes were needed for
90%; the schema reached it with 73 because matching is by ordered containment,
so a short template legitimately absorbs its longer siblings.

## Copying this

**Coverage here is 97.7% because the schema leans on containment, with broad
fallbacks** — `warning`, `error`, `connect from`, `disconnect from`. Those are
deliberate and they are why 73 classes beat the measured 206. They are also why
the class-specific `normal` and `retention` judgements only apply where a
specific class matched. If you need per-event retention to be meaningful, drop
the fallbacks and expect to declare the full vocabulary. The trade is explicit:
cheap coverage against a thin long tail, or expensive coverage against precise
per-event value.

**Do not add classes you have not seen in your own data.** Run
`logschema inspect` on your logs and copy shapes from what it reports.

**The identifiers were stripped, and that is why the templates look generic.**
The draft carried classes naming specific peer hostnames and the local mail
domain — real network identifiers, not masked ones. They were removed rather
than masked, because each was redundant with a generic shape already declared.
Run your own check before publishing a schema:

```
grep -nE '<your-domain>|<peer-hostname>|<credential-fragment>' schema.yaml
```

A credential fragment worth naming: postfix writes the literal text
`SASL LOGIN authentication failed: <base64>` on every failed submission. That
base64 decodes to the SASL challenge label. It is in the log, it is not
PII-shaped, and a redaction pass looking for emails and IPs will not catch it.

## What this schema does not solve

A mail log has **two** entity axes and this format declares one. `(ident, pid)`
is the connection: an smtpd child serving one client. The message is a
different thing, identified by the postfix queue id that appears inside
`message`. One `identity_key` cannot express both, and the queue id is not a
field here because it is a Postfix convention inside the message text, not
something syslog delimits.

Measured on this corpus: one queue id appears across 10 lines spanning six
distinct programs — injection, cleanup, the content filter, the queue manager,
the filter's own re-injection, and final local delivery. That is the join a
customer asking "what happened to my message" needs, and the current schema
cannot express it.