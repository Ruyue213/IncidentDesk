# Contributing to IncidentDesk

Thank you for improving a local-service diagnostic tool. Start with a reproducible symptom and an observable outcome: a synthetic failure, a retrieval miss, an interrupted operation, or a service connection that cannot be collected safely. Discuss scope before adding a provider or deployment mode.

The current contract is one operating-system user, a loopback web service, a single worker, and explicit approval of registered actions. Changes must preserve project/file scope, frozen previews, approval digests, pre-execution checks, durable receipts, and uncertainty after an interrupted external effect. A model must not acquire arbitrary shell execution.

## Development checks

Use Python 3.12, Node.js 24, and the committed dependency locks:

```sh
uv sync --frozen --extra rag --group dev
npm ci --prefix frontend
uv run --frozen --extra rag ruff check .
uv run --frozen --extra rag ruff format --check .
npm run build --prefix frontend
npm test --prefix frontend
uv run --frozen --extra rag pytest -q
uv build --out-dir dist
uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes --output-file dist/incidentdesk-constraints.txt
python scripts/check_release.py --wheel dist/incident_desk-0.3.0b1-py3-none-any.whl --constraints dist/incidentdesk-constraints.txt
```

The optional `rag` dependencies enable local embeddings. Keep `--extra rag` on `uv run` when you need them; uv can remove unselected extras. Core tests and CI use injected embeddings and scripted models without downloading weights or calling a paid provider. Actual embedding experiments require an explicitly downloaded fixed model; report model revision, corpus, queries, ranking settings, and results.

Tests should verify behavior that can fail independently of the implementation: scope escapes, corrupted citations, changed files between preview and approval, persistence across restart, counter resets/gaps, index version isolation, and independently installed artifacts. Do not add a test that only repeats a harmless implementation detail.

## Pull requests and reports

Describe the concrete trigger, previous behavior, resulting behavior, and the checks you ran. Include a minimal synthetic case and expected result when changing diagnosis, retrieval, or recovery. Report retrieval quality separately from end-to-end diagnosis quality; scripted model success is a reliability check, not a model accuracy measurement.

New model adapters need a full tool-call/return round trip, reasoning-field handling, bounded requests, usage/error handling, and cost reservation checks. Real paid tests must be explicit and use a separate budget; never put provider credentials into CI.

Do not submit `.env`, API keys, state databases, model weights, real logs, private prompts/replies, backups, personal project paths, or third-party project source without authorization. Issues, screenshots, recordings, and build artifacts have the same requirement. Prefer synthetic data; automatic redaction cannot identify every private business value. Follow [SECURITY.md](SECURITY.md) for vulnerabilities.

## License and provenance

This project is licensed under **Apache-2.0**. By contributing, you agree to license your contribution under Apache-2.0 and confirm that you have the right to submit it. Do not copy third-party source, datasets, model weights, or patches unless the upstream terms permit it; preserve required notices and document provenance. Third-party dependencies and the optional embedding model keep their own licenses.

## Local maintenance

Use `incidentdesk stop` before backup and stop managed services before restoring. `incidentdesk backup --archive /absolute/private-backups/state.zip` includes credentials and evidence, uses user-only permissions, and is not encrypted or signed. `incidentdesk restore --archive ...` requires the same app version and refuses a nonempty target unless `--replace` is explicit. Restored PID metadata cannot grant process ownership. Model weights are excluded and can be downloaded again by revision; external project files require a separate backup.
