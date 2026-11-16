# Isolated live service validation

This report records real HTTP component integration against a user-supplied,
compatible SocialEngine executable on 2026-11-16. The exploratory run retained a
failed overload check; two subsequent runs passed all four scenarios after the
workload was changed and its parameters frozen. There were **zero cloud model
API calls** across these three runs.

These are post-selection reproducibility checks of collectors and registered
business probes. They do not measure blind diagnosis accuracy, model quality,
production capacity, availability, or remediation safety. The separate live
service plus cloud-model smoke test is outside this report.

The machine-readable record is [live-service-validation.json](live-service-validation.json).
It retains every scenario's pass/fail checks, bounded load results, cleanup
results, execution times, and hashes of the private original result records.
It includes no executable, private source code, patches, raw logs, local
filesystem locations, or process identifiers.

## Prerequisite and isolation

The compatible target executable is **not distributed with this repository**.
This integration check cannot run from the public repository alone. Operators
must supply a locally built executable with the CLI and HTTP contracts used by
[the harness](../../scripts/evaluate_live_service.py). Set `SOCIALENGINE_BINARY`
to its location in your own environment, then run:

```sh
uv run python scripts/evaluate_live_service.py --binary "$SOCIALENGINE_BINARY" \
  --nodes 5000 --degree 8 --seed 42 --requests 192 --concurrency 32
```

The harness starts temporary executable copies on fresh loopback ports with
empty service roots. It disables Redis, supplies no WAL or snapshot, and only
terminates its own launched children. Scenario definitions and result files
remain outside the service root accessible to diagnostic tools. Raw execution
artifacts stay in ignored local state; the public files here are sanitized
summaries.

Target executable SHA-256:

```text
3f46669e897a7be11c4143e382143bf39d451bcddc4cc00e40ea3bf2f4782c6f
```

## Recorded outcomes

All runs used 5,000 graph nodes, degree 8, seed 42, and 192 finite load requests
at concurrency 32. Normal service instances used 2 reactors, pool 2 and queue
64. The overload instance used 2 reactors, pool 1 and queue 1. The load deadline
was 30 seconds. The harness restricts nodes to 100–20,000, degree to 1–32,
requests to 16–512, and concurrency to 1–64.

| Run | Read-only load body | Healthy | Contract mismatch | Stopped target, sibling alive | Overload | Load HTTP 200 / 503 | Exported 503 delta |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Exploratory | `{"type":"dist","a":1,"b":2}` | Pass | Pass | Pass | **Fail: no rejection** | 192 / 0 | 0 |
| Frozen replication 1 | `{"type":"sd","a":1}` | Pass | Pass | Pass | Pass | 165 / 27 | 27 |
| Frozen replication 2 | `{"type":"sd","a":1}` | Pass | Pass | Pass | Pass | 168 / 24 | 24 |

Each run accounted for all 192 load responses with no transport errors. All
three temporary children in each run stopped with exit code 0. The two frozen
replications used the same executable hash, graph seed, request body, launch
parameters and load bounds. Their differing rejection counts are retained.
The exploratory failure is not included in a success percentage or discarded.

The four scenarios established the following within each isolated run:

- **Healthy:** the registered self-distance assertion passed, the managed target
  identity was running, only that target was attributed, and at least two
  historical samples existed.
- **Health succeeds, business assertion fails:** health and business HTTP both
  returned 200, but a deliberately inconsistent registered expectation of
  self-distance 1 failed against the response 0. The probe rejected business
  success, and the changed profile's history remained version-scoped. This is
  a controlled contract mismatch, not evidence of a defect in the target code.
- **Stopped target with a live sibling:** the stopped target was reported as not
  running with no attributed process. The sibling remained business-healthy
  and appeared only among other observed instances; the target probe was
  unavailable.
- **Bounded overload:** the two frozen runs observed HTTP 503 responses and
  matching exported 503 counter increases. The registered business probe
  passed after load stopped. The exploratory workload produced no 503 and
  therefore failed this scenario as intended by its acceptance gate.

## Interpretation limits

The workload was selected after the exploratory run failed to exercise queue
pressure. The two later runs are repeat observations after that selection;
they are not a hidden test set. Host scheduling, client effects and hardware
were not controlled, so neither the rejection counts nor recorded client
latencies support a throughput or capacity claim. HTTP counts in the table
refer only to the bounded load, excluding monitoring and probe requests.

This report validates concrete source collection, business assertions,
instance isolation, and retained samples for the stated executable and
parameters. Separate unit tests cover additional history semantics such as
counter resets, known process restarts, units and collection gaps; the live
scenarios here do not independently validate all of those cases.
