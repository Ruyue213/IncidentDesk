# Product direction and release gates

IncidentDesk serves developers investigating performance or availability failures in services running as local processes. The product outcome is a traceable path from symptom, to scoped evidence, to an approved intervention, to a bounded business check. A high-quality reproducible user report is more useful than an unmeasured feature count.

## v0.3 engineering beta gate

A release must meet all applicable checks below. Workflow files or planned functionality do not count as evidence of a passing run.

- A clean source export contains no private runtime state, credentials, personal paths, real customer cases, external project code/patches, or model weights. Historical private development records remain outside the public export.
- Committed locks install on Python 3.12. Backend and frontend checks pass without API credentials or an existing target service. Linux and macOS CI results are recorded; unsupported platforms are named.
- Both wheel and sdist build. A fresh environment installs the wheel from an unrelated directory and serves its included UI/assets. Registration, knowledge indexing/search, historical observation endpoints, scripted task completion, stop, backup, and restore work without checkout-relative paths.
- Project knowledge is scoped and versioned. Tests cover stale registration/index generations, provenance, secret/path exclusions, missing dense capability, and declared fallback. An actual fixed embedding model is evaluated separately from injected vectors.
- Metric windows report collection coverage, gaps, retention, resets, and unavailable rates. Startup/health success is distinguished from a configured business probe result.
- Approval preview, digest, project version, target identity, file hash, and effect receipts remain checked. Recovery never blindly replays an operation whose external result is unknown.
- Model cost reservations persist across restarts. A real provider smoke test is explicitly budgeted and reported separately from no-provider release checks.
- README and release notes state what ran, what remains unverified, data egress, state locations, maintenance procedure, and the single-user boundary. No synthetic score is presented as production accuracy.

## Adoption sequence

1. **Make one investigation reproducible.** Ship a small synthetic local service, a deterministic failure trigger, expected observables, and a reset procedure. Keep the scripted reliability route and real-LLM route visibly distinct. A new user should reproduce the same symptom on a clean checkout and understand every proposed action.
2. **Validate independent installations.** Collect opt-in, redacted reports from several operators using their own service and machine. Measure time to first collected evidence, missing-source reasons, installation failures, false recovery signals, and cases where the operator rejected the diagnosis. Publish the denominator and unsuccessful cases.
3. **Measure retrieval changes.** Use a versioned, redistributable corpus with queries for exact errors, paraphrased symptoms, cross-language terms, and genuinely absent answers. Compare lexical, dense, and hybrid retrieval on Recall@k, reciprocal rank, latency, memory, and no-answer behavior. Prefer defaults supported by those measurements; retain explicit degradation.
4. **Broaden adapters only from evidence.** Add a new service or observation source with a documented registration contract, bounded read-only business probe, failure injection, and tests. Docker/remote execution or another model provider requires an explicit threat/support review and its own release checks.
5. **Turn verified cases into public material.** Publish short, reproducible case studies with synthetic or explicitly redistributable data, source revisions, settings, evidence, unsuccessful hypotheses, and final verification. Provide a small video as a release asset after checking it for private data. Keep the repository small enough to clone quickly.

Collect field evidence before choosing shared deployment defaults. Record queue delay and completion percentiles at known concurrency, publish diagnostic review coverage and independent incident labels, and distinguish attempted operations, mistaken actions, and verified recovery. An operator outcome is useful evidence for a single investigation. Measuring accuracy requires independent review and a recorded sampling policy.

A shared deployment would need authenticated identities, authorization on every project/run/evidence/action, tenant-scoped credentials and storage, secret rotation, and a provider egress policy. Multiple worker processes would also need renewable job leases and fencing so that an expired owner cannot write effects. Recheck these boundaries with representative incidents and repeated failures before accepting shared deployment.

## Deliberate non-goals for this beta

Multi-user authentication/tenancy, public-network deployment, arbitrary autonomous remediation, broad shell access, cluster orchestration, organization-scale RAG, and provider-agnostic compatibility are not implied by this release. Complete offline LLM inference is not provided by a local embedding model.

GitHub popularity cannot be guaranteed. We will prioritize issues that improve an independently reproducible investigation or close a demonstrated safety/recovery gap. We do not buy stars, automate engagement, spam communities, or substitute presentation metrics for engineering evidence.
