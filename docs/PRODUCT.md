# Product boundary and release gates

IncidentDesk helps one developer diagnose performance regressions and availability failures in an authorized local process. Its useful output is an evidence-backed investigation: what was observed, which explanations fit or conflict, what is missing, and the next bounded check. A successful model response alone is not a recovered service.

## Supported workflow

1. Install and start on a loopback address. Register the project root, exact health/metrics/stats endpoints, optional logs, reviewed knowledge documents, and an optional fixed read-only business probe.
2. Configure the DeepSeek endpoint, model, reasoning setting, request limits and spending limits. A saved configuration is not a successful connection test.
3. Build an immutable knowledge generation. Start collecting observations before expecting historical comparisons. Collection cannot reconstruct past incidents.
4. Submit an incident question and a time window. The task freezes project, model and knowledge versions. The agent selects bounded tools, cites snapshots and separates runtime facts from runbook guidance.
5. Inspect quotes, counterevidence, coverage and uncertainty. A process running and a health endpoint returning 200 are independent of business assertions and latency limits.
6. For a restart or registered configuration edit, inspect the exact frozen command or diff and approve that operation. The executor rechecks identity, permissions and content hashes. Interrupted external effects remain UNKNOWN until inspected; they are never blindly replayed.
7. Follow up with a new evidence window. Preserve the original result and operation receipt, including failures.

## First real integration

The local validation target is a separately owned graph-query process, SocialEngine. Its repository, source changes, binary, original documentation, runtime logs and state are not distributed with IncidentDesk. A user supplies their authorized binary to optional integration checks. Public retrieval runbooks and query sets are independently authored synthetic material.

This release targets local single-process services and the supported fixed graph-query contract. It does not offer Kubernetes, Docker orchestration, remote fleet management, shared accounts, public exposure or unattended remediation. Broader adapters should be added only with a real integration, capability restrictions and reproducible contract tests.

## Acceptance evidence

| Gate | Required evidence |
| --- | --- |
| Reproducible distribution | Locked source build plus a wheel installed in a fresh environment outside the checkout; UI, API, task completion and backup/restore exercised |
| Persistence and execution | Idempotent submissions, frozen approvals, cancellation races, crash recovery and non-replay of ambiguous effects |
| Retrieval | Frozen development and holdout sets; lexical, dense and hybrid under the same corpus/filter/k; all misses and latency retained; model revision and dataset digest published |
| Observations | Real HTTP scrape persistence, units/types, minimum samples, counter resets, retention and gaps; no fabricated pre-collection history |
| Business availability | Exact registered request and JSON assertions; health-only success cannot imply business success |
| Real integration | Isolated processes and ports, target identity, stopped-target isolation and business assertion failure; optional load experiment records its actual outcome |
| Model integration | Metered real model tools and validated quotes; paid checks are explicit and share the cumulative local budget ledger |
| Public release | New sanitized history, no target source/data/credentials, license and third-party notices, remote CI results linked |

## Measurement limits

Small synthetic retrieval gains are not an incident diagnosis accuracy claim. A load run on one laptop is not a capacity SLA. Scripted models verify workflow reliability; they do not measure reasoning quality. A local known-cost estimate is not the provider's invoice. Engineering confidence comes from publishing these distinctions and preserving negative results.

The roadmap prioritizes repeatable user value and maintainable adapters. GitHub stars are an external adoption outcome, not an acceptance criterion and not a guarantee.

## Application health contract

`/health` checks the application database. `/readyz` requires a reachable database and a fresh live worker heartbeat, returning HTTP 503 when either is unavailable. `/api/readiness` is the UI's status panel: it also reports model configuration and observation-sampler state, and its HTTP 200 is not a deployment readiness guarantee. These routes describe IncidentDesk itself, not the external model's connectivity or the diagnosed service's health. A failing target is a valid subject for an otherwise ready diagnostic application.
