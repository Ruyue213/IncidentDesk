# Engineering beta verification

These are separate forms of evidence. Passing a workflow test does not establish diagnosis accuracy.

| Check | Recorded result | Scope |
| --- | --- | --- |
| Local backend regression | 310 passed, 1 skipped | macOS, Python 3.12; includes an explicitly supplied real-service binary; skip is Linux procfs |
| Frontend state contracts | 11 passed | Submission retry, frozen approval, durable events and stale-response handling |
| Browser interaction contracts | 7 passed | Real Chrome, intercepted synthetic API; includes knowledge indexing, degraded retrieval, history gaps and cross-project races |
| Browser workflow/recovery | 17 passed | Real isolated API/SQLite/Chrome with a network-blocked scripted model; zero provider calls |
| Distribution | Fresh virtual environment and unrelated working directory passed | Bundled UI/assets, project registration, knowledge, history, business probe, scripted task, start/stop and backup/restore |
| Real service components | First exploratory load 3/4; two fixed-parameter replications 4/4 | External user-supplied executable, isolated processes; no model calls; negative result retained |
| Real cloud agent smoke | SUCCEEDED, 6 model calls, 12 tool calls, 6 validated quotes, no repair or action | A single authorized local service; explicitly not a diagnosis accuracy estimate |

Real service component parameters, status counts, cleanup and limitations are in [the component report](live-service-validation.md) and [machine-readable summary](live-service-validation.json). The executable, source changes, original documentation, logs and runtime state are not distributed. The public repository alone cannot reproduce that target-specific test without a compatible executable.

The [cloud-agent summary](live-model-validation.json) publishes outcome metadata only. It exercised project knowledge, retained metric history and a fixed business assertion through the actual agent. The report acknowledged incomplete historical coverage and the absence of a comparable performance baseline. It does not establish recovery, load capacity or performance-regression classification quality.

The retrieval dataset was frozen before evaluation. [The retrieval protocol and results](../../evals/retrieval/README.md) distinguish the early implementation baseline from final-code regression. Every failed or degraded query remains in the denominator. A 12-query synthetic holdout is not production accuracy.

The release wheel and source archive are built from the clean public checkout, not the private development history. Release dependency constraints and SHA-256 checksums accompany them. Remote CI and its exact revision are linked by the GitHub release once executed; the local results above are not a claim that unrun CI passed.
