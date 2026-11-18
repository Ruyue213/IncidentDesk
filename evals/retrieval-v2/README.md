# Retrieval v2: frozen local reranking comparison

This benchmark is original synthetic material, not production incidents, user
feedback or an independent blinded evaluation. The same development collaborator
authored the corpus and both splits. No v1 inspected holdout is reused.

## Materials and freeze

- 28 original English runbooks/counterexamples, yielding 84 actual chunks.
- Development: 24 queries across 6 mechanism groups; 18 answerable, 6 no-answer.
- Acceptance: 32 queries across 8 different mechanism groups; 24 answerable,
  8 no-answer. The mechanism groups are disjoint; the complete corpus is available
  to both splits. Each group contains an identifier query, English paraphrase,
  Chinese paraphrase and an explicitly unanswerable query.
- Exact source-character spans have grades 3/2. A full gold quote must occur in
  the correct chunk/path. Merely retrieving the right document earns no credit.
  Every answerable query has two gold units. All 84 query-span labels were
  statically resolved to complete chunks before inference.
- `freeze-v2.json` froze material hashes at **2026-11-17T04:40:39.249734+00:00**,
  before the first v2 neural trial. Aggregate SHA256:
  `dfad3c81b61cfd6e4abd799bd57dd42b5c49f43377daa33a11d6e1823bdfd832`.
  Its commit is the source base, not a claim that the new materials were committed;
  individual hashes identify the uncommitted material exactly.

`protocol.json` and `run-plan-v1.json` specify the experiment and adoption screen.
They were saved before model trials. Budgets stayed **N=50 per channel, M=20,
K=5, hybrid RRF k=60**. Every off/on pair has the same ordered pre-rerank pool.
The local CPU fp32 CrossEncoder uses pinned
`cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` revision
`1427fd652930e4ba29e8149678df786c240d8825`; full configuration and library
versions are in each compact report.

## Recorded results

All planned ordinary runs completed: development 96 requests/48 paired
comparisons, acceptance 128 requests/64 paired comparisons, with exact candidate
identity in every pair. Both rounds' rankings and metrics are retained; no best
round was selected. The table shows the predeclared `cache_warm_repeat` round
and **answerable-only** quality; all-query denominators are also in the reports.

| Split / mode | Candidate span Recall@N | Pool span Recall@M | Span Recall@5 | MRR@5 | Chunk nDCG@5 | Hard-negative Top5 exposure |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Dev / off | .9167 | .7778 | .5000 | .4926 | .3754 | 13/18 |
| Dev / on | .9167 | .7778 | .5833 | .5898 | .5071 | 11/18 |
| Acceptance / off | 1.0000 | .9375 | .5625 | .5813 | .4186 | 19/24 |
| Acceptance / on | 1.0000 | .9375 | .7917 | .7986 | .7361 | 20/24 |

Acceptance improves ranking quality but fails the frozen hard-negative
nonregression gate. This gate measures the presence of a labeled counterexample
document in the final Top5, not whether a generated diagnosis is wrong. Its
increase is still sufficient to reject automatic adoption under the recorded
screen. **The default remains `off`; enabling local reranking is an explicit
operator choice.** Development passing the screen does not override acceptance.

All no-answer queries returned context in both modes: dev 6/6, acceptance 8/8,
in both rounds. Correct-empty-retrieval is zero. There is no calibrated abstention
threshold. A retrieval result is not an answer, and this experiment makes no
diagnosis-accuracy claim.

Acceptance CPU/cache-warm-repeat timing:

| Mode | Median total | p95 total | Median model load | Median inference | CE child peak RSS max |
| --- | ---: | ---: | ---: | ---: | ---: |
| Off | 13.44 ms | 15.16 ms | — | — | — |
| On | 2502.61 ms | 2733.42 ms | 1964.56 ms | 117.27 ms | 877.63 MiB |

Each CE call starts a **new subprocess and loads weights**; the second round
means OS file-cache warm, never a resident warm CE model. First pass also does
not guarantee an empty operating-system cache. Setup/indexing is measured
separately. Parent RSS is a lifetime high-water value; child RSS is the CE
subprocess high-water value, not a simultaneous memory sum. These measurements
are for this machine and synthetic corpus, not a production SLA.

An additional intentionally missing-model trial retains all **96/96 degraded
requests**. Every strict metric is zero; returned lexical-fallback quality is
reported separately. Empty failed retrieval is never rewarded as correct
abstention. Ordinary runs have no failure/degradation.

## Audit artifacts

- `results/development-01.json`, `results/acceptance-01.json`, and
  `results/models-absent-01.json` are compact exports. They include complete
  category counts, all rounds' denominators/metrics/timings/RSS/truncation,
  source/plan/data hashes, per-query before/after Top5 IDs/scores, final IDs,
  exact gold-unit ranks and raw-report hashes. They omit candidate bodies,
  credentials, real run identifiers and absolute local paths.
- Full private reports/journals retain all candidate content and rankings under
  the local evidence directory. The raw report filename/hash in a compact export
  identifies the source; full reports are not intended for public distribution.
- `source-freeze-dev-01.json` and `source-freeze-acceptance-01.json` identify the
  exact evaluation code used. Those requests used unchanged source throughout.
- A final input-validator fix now rejects additional unlisted corpus files,
  as well as changed/missing frozen files. Actual evaluated corpus had exactly
  the frozen files, so ranking/scoring/results are unchanged. No model rerun or
  parameter/label/gate adjustment was made for this guard. The original driver
  bytes are preserved in `runner-snapshots/evaluate_reranking-01.py.txt`, matching
  both original source freezes. `source-freeze-validator-02.json` describes the
  current driver after the guard and is **not** a new neural result.

## Reproduce

Install the optional local RAG dependencies and explicitly install the pinned
embedding/reranking models first; the evaluator never downloads them or calls a
cloud model. From the repository root, use new output names:

```sh
python scripts/evaluate_reranking.py --validate-only
python scripts/evaluate_reranking.py --freeze-code /tmp/retrieval-v2-source.json
python scripts/evaluate_reranking.py --split development \
  --model-dir /path/to/incidentdesk-home --code-freeze /tmp/retrieval-v2-source.json \
  --output /tmp/retrieval-v2-development.json
python scripts/evaluate_reranking.py --split acceptance \
  --model-dir /path/to/incidentdesk-home --code-freeze /tmp/retrieval-v2-source.json \
  --output /tmp/retrieval-v2-acceptance.json
python -m scripts.summarize_reranking --input /tmp/retrieval-v2-acceptance.json \
  --output /tmp/retrieval-v2-acceptance-compact.json
```

Keep every attempt. The acceptance set is now observed; future changes must not
tune against it and claim it is unseen. New ranking strategies require a new
independent frozen acceptance split. Unit checks cover hash tampering, unlisted
corpus candidates, exact-span rather than document credit, discount/duplicate
handling, and all-failure/no-answer denominators without running models.
