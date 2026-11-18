# Operator outcomes

An execution status records whether the workflow finished. It does not establish
that a diagnosis is correct or that a service recovered. Outcome records are
explicit operator annotations, with reasons and references to captured evidence.
No workflow, model, probe or successful action automatically supplies these labels.

`POST /api/runs/{run_id}/outcome` accepts a JSON body:

```json
{
  "diagnosis_assessment": "inconclusive",
  "diagnosis_reason": "The available observations do not identify a cause.",
  "recovery_assessment": "not_attempted",
  "recovery_reason": "No intervention or recovery verification has occurred.",
  "verification_references": [],
  "expected_revision": 0
}
```

Diagnosis choices are `confirmed`, `rejected`, `inconclusive`. Recovery choices
are `verified`, `failed`, `not_attempted`. Reasons are mandatory. Confirming or
rejecting a diagnosis requires an actual final diagnosis and captured references.
Attempted recovery requires runtime evidence; a runbook alone cannot verify it.
Only terminal runs accept an annotation. A reference uses the task's exact
`id`, `version` and an unchanged `quote` from the snapshot's content.

For a terminal registered project, `POST /api/runs/{run_id}/verify-business`
runs its fixed read-only business assertion and saves a fresh snapshot in the
same task. Its result is available under `GET` at the same path and can be cited
in an outcome. A changed project version requires a new diagnosis task. When an
annotation cites these explicit verification snapshots, its recovery choice must
agree with the newest cited check's `business_ok`. Ordinary runtime references
remain available for a separately reasoned manual assessment. A passing finite
assertion covers only its registered inputs, not overall availability or causality.

`GET /api/runs/{run_id}/outcome` returns the latest annotation. Edits append a
revision; submit the revision just read as `expected_revision`. Concurrent stale
edits return 409. An identical retry of the latest payload returns the same
record, including after an application restart. History remains in the local
application database and its backup. These records are marked
`source=operator_supplied`, `independent_verification=false`.

`GET /api/outcomes/aggregate` exports counts and ratios for real-project mode by
default. `?mode=fixture` or `?mode=all` is explicit. The export uses a fixed
whitelist and contains no run/project identifiers, paths, reasons, quotes or
other free text. It counts the latest annotation for each task, includes
unannotated terminal tasks in coverage, and shows an absent denominator as null.
Confirmation share is an operator annotation statistic, not diagnosis accuracy.
The module does not infer independent incidents/users or manufacture long-term
data. Follow-up tasks may refer to the same incident. Export is local; nothing
is uploaded automatically.

For an isolated reproducible plumbing check, run:

```sh
python scripts/evaluate_outcomes.py --output /tmp/incidentdesk-outcome-check-01
```

This starts its own API, worker and synthetic HTTP service with temporary data,
uses a scripted model, blocks non-loopback connections in API/worker, and never
reads a real credential or starts SocialEngine. It demonstrates HTTP health
success with a failing business assertion, an explicit failed annotation, a
test-owned state change, a fresh passing assertion, a recovery revision,
conflict rejection, process restart and aggregate privacy. It is not evidence of
real diagnosis accuracy or real service recovery.
