# Frozen synthetic retrieval benchmark v1

These twelve original runbooks describe generic service failure mechanisms. They contain no user project code, private documents, production logs or cloud-model output. Twenty-four manually authored queries have equal-size development and holdout splits, each with four exact-identifier, four English paraphrase and four Chinese paraphrase queries. This deliberately tests cross-language retrieval over English documentation; it is not an estimate of any production query distribution.

Freeze the corpus and both label sets with `scripts/evaluate_retrieval.py --freeze` before the first result. Do not tune chunking, fusion or labels on holdout results. Changes require a new version and another untouched acceptance set. The current holdout is public and supports regression comparison, not a claim of uncontaminated future model evaluation.

Run from the repository root after explicitly installing the optional `rag` dependency and downloading the pinned local model:

```sh
.venv/bin/python scripts/evaluate_retrieval.py --split development --local-dir .local --require-dense --output .local/evals/retrieval-development.json
.venv/bin/python scripts/evaluate_retrieval.py --split holdout --local-dir .local --require-dense --output .local/evals/retrieval-holdout.json
```

All three modes use the same redacted chunks, generation, filters, document-diversity rule and top-five result budget. Hybrid fuses at most fifty candidates per channel using RRF with k=60. Exact cosine scans the same bounded generation as lexical BM25. Metrics operate on document relevance within the top-k **chunk slots**: a repeated document gains no further recall or nDCG credit. Recall@1/3/5, MRR@5 and nDCG@5 include failed queries as zero; availability and degraded queries are also recorded separately. Latency includes query encoding but excludes one-time indexing and model loading, which are reported independently. Query kinds are reported separately to reveal cross-language gains and exact-term regressions.

An absent local model produces an explicit degraded lexical result. `--require-dense` fails the run if genuine local dense inference did not execute; degraded results never count as a successful dense comparison. No model download, cloud LLM request or business process action happens during evaluation. Results are a small synthetic capability test, not end-to-end diagnosis accuracy, proof of root causes or a production service-level objective.

## Final-code regression

The v0.3 code was evaluated from public source commit `9a054f93df305554a1a4b2567a610b38c40033ca`, with actual imports verified to use that source tree. Complete [development](regression-v0.3-development.json) and [holdout](regression-v0.3-holdout.json) reports retain code hashes, dependency versions, model revision, per-query rankings, failures, timings and metrics. Their evaluated retrieval-file hashes were checked against that source tree before publication. These are new runs over the unchanged frozen corpus and queries, not revised early measurements.

| Final-code holdout, 12 queries | Recall@5 | MRR@5 | nDCG@5 | Warm-query p95 |
| --- | ---: | ---: | ---: | ---: |
| Lexical | 0.6667 | 0.6667 | 0.6667 | 6.21 ms |
| Dense | 0.9167 | 0.8333 | 0.8552 | 34.10 ms |
| Hybrid | 0.9167 | 0.8750 | 0.8859 | 32.89 ms |

All 72 mode/query executions across both splits completed without failure or degradation. Quality metrics match the early run: development Recall@5 is 0.6667 for lexical and 1.0 for dense/hybrid; holdout hybrid retrieves the relevant document within five chunk slots for 11 of 12 queries. The Chinese business-correctness query (`holdout-09`) is still missed. The generation contains 67 chunks. Model loading/warmup took 7.66 seconds for development and 6.68 seconds for holdout; indexing took 1.18 and 1.19 seconds. Warm-query latency excludes both costs and varies between runs; these values are not a production latency guarantee.

No labels, chunking or fusion parameters were adjusted after inspecting the earlier holdout. Because these queries had already been inspected, this is a final-code **regression**, not a new unseen acceptance set or an estimate of production diagnosis accuracy. No cloud model was called and no real project was accessed. The early report below remains unchanged.

## Early implementation record

The **early implementation** local E5 run is recorded in [baseline-v1.json](baseline-v1.json), with dataset, code and dependency hashes. Its recorded Git commit belongs to private development history and cannot be resolved in the clean public repository. The per-file hashes identify the actual evaluated files; this report predates subsequent indexing-concurrency and UI fixes and is not validation of final release code. The final-code regression above uses the same frozen inputs and separate reports without replacing these original measurements. No labels or retrieval parameters were changed after holdout inspection. The frozen corpus produced 67 chunks.

| Early holdout, 12 queries | Recall@5 | MRR@5 | nDCG@5 | Warm-query p95 |
| --- | ---: | ---: | ---: | ---: |
| Lexical | 0.6667 | 0.6667 | 0.6667 | 1.12 ms |
| Dense | 0.9167 | 0.8333 | 0.8552 | 13.25 ms |
| Hybrid | 0.9167 | 0.8750 | 0.8859 | 11.20 ms |

All modes executed successfully. This run's gains mainly reflect the deliberately included cross-language queries. The Chinese health-versus-business-correctness query (`holdout-09`) remained outside the hybrid top five: real embedding availability is not a guarantee of useful recall. Development Recall@5 was 0.6667 for lexical and 1.0 for both dense and hybrid. Model loading and warmup took 20.7 seconds in the first process and 2.7 seconds in the second; indexing took 428 ms and 382 ms. The query timing table excludes those costs and is specific to this machine and tiny corpus.
