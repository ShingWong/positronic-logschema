# logschema — agent startup

`logschema` builds and validates log-ingestion schemas. It is a standalone
tool. It does not require a particular coding agent, and it does not require a
memory substrate. For full usage see `logschema help agent-detail`.

## Starting a new project

1. Ask the user for the project name and the location of the log data. Do not
   guess either one, and do not pick a default path.
2. Run `logschema project init --name <name> --location <path>`. It creates
   the directory and writes every file in the manifest below. Do not create
   these files by hand.
3. Make the project directory the working directory for the rest of the
   session. If your harness cannot move the session, carry the absolute path
   on every command below; nothing in this loop depends on the cwd.
4. Run `logschema inspect <path>` and relay its questions to the user.

## Manifest written by `logschema project init`

| file | purpose |
|---|---|
| `README.md` | What this project is, the software, the data source, and how to re-run every step. |
| `SCHEMA.md` | Fields with roles and meanings, event classes with meanings, the identity key, and the evidence that chose it. |
| `FINDINGS.md` | The class inventory: frequency, chain participation, identity resolution, content mass, and the proposed disposition per class. |
| `DECISIONS.md` | Every choice the user made, with its reason and the date. Exists so the next person does not re-derive them. |
| `VALIDATION.md` | What was checked, what failed, and the iteration history of the build loop. |
| `schema.yaml` | The machine artifact: fields, roles, types, classes, dispositions. |
| `conformance/` | The conformance test, plus the negative fixtures that MUST fail. |

## Three rules

- Do not answer the tool's questions yourself. Relay them to the user
  verbatim, with every option and its evidence, including the rejected ones.
- Do not invent, rename, or substitute a field to make a check pass.
- If a loop reports `exhausted`, stop and report it. That is a valid outcome,
  not an obstacle to route around.

These three are not a substitute for enforcement. The tool does not depend on
your compliance: `logschema validate` is the only thing that can report a
schema valid, and ingestion refuses a schema with no validation verdict on
file. The rules are here so that a refusal makes sense to you and to the user.

## What happens next

After `logschema inspect`, you enter the build loop: draft, check against real
data, revise, write the conformance test, repeat until validated. That loop is
described in full in `logschema help agent-detail`. Read it before step 4
above, not after.
