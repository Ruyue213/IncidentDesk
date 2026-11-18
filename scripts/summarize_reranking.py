"""Export a compact, text-free ranking trace from one immutable synthetic trial."""

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.evaluate_reranking import (
    canonical_hash,
    matches_span,
    sha,
    validate_dataset,
    write_new,
)


def compact(raw_path, output):
    raw = json.loads(Path(raw_path).read_text())
    validation = validate_dataset()
    queries = {query["id"]: query for query in validation["splits"][raw["split"]]}
    rows = raw["rows"]
    pairs = {}
    for row in rows:
        pairs.setdefault((row["round"], row["query_id"]), {})[row["mode"]] = row

    def ranking(rows):
        return [
            {key: row[key] for key in ("id", "rank", "path", "scores") if key in row}
            for row in rows[:5]
        ]

    def gold_ranks(query, rows):
        return {
            label["unit_id"]: [rank for rank, row in enumerate(rows, 1) if matches_span(row, label)]
            for label in query["relevant_spans"]
        }

    comparisons = []
    for (round_name, query_id), pair in pairs.items():
        off, on = pair["off"], pair["cross_encoder"]
        query = queries[query_id]
        before = off["candidates"].get("before_rerank", [])
        after = on["candidates"].get("after_rerank", [])
        comparisons.append(
            {
                "round": round_name,
                "query_id": query_id,
                "mechanism_group": query["mechanism_group"],
                "kind": query["kind"],
                "language": query["language"],
                "answerable": query["answerable"],
                "same_candidates": off["paired_candidates_equal"] and on["paired_candidates_equal"],
                "candidate_pool_count": len(before),
                "candidate_pool_ids_sha256": canonical_hash([row["id"] for row in before]),
                "before_rerank_top5": ranking(before),
                "after_rerank_top5": ranking(after),
                "gold_unit_ranks": {
                    "fused": gold_ranks(query, off["candidates"].get("fused", [])),
                    "before_rerank": gold_ranks(query, before),
                    "after_rerank": gold_ranks(query, after),
                    "final_off": gold_ranks(query, off["final"]),
                    "final_on": gold_ranks(query, on["final"]),
                },
                "modes": {
                    mode: {
                        "valid": row["valid"],
                        "status": row["status"],
                        "error_code": (row.get("error") or {}).get("code"),
                        "final_ids": [chunk["id"] for chunk in row["final"]],
                        "strict": row["strict"],
                        "returned_quality": row["returned_quality"],
                        "elapsed_ms": row["elapsed_ms"],
                        "parent_peak_rss_mib": row["parent_peak_rss_mib"],
                        "reranker": {
                            key: row["reranker"].get(key)
                            for key in (
                                "requested",
                                "effective",
                                "status",
                                "pair_count",
                                "latency_ms",
                                "model_load_ms",
                                "inference_ms",
                                "peak_rss_mib",
                                "truncation",
                                "isolation",
                            )
                        },
                    }
                    for mode, row in pair.items()
                },
            }
        )
    category_metrics = {}
    for dimension in ("language", "kind", "mechanism_group"):
        for category in sorted({query[dimension] for query in queries.values()}):
            for round_name in raw["run_plan"]["rounds"]:
                for mode in ("off", "cross_encoder"):
                    selected = [
                        row
                        for row in rows
                        if row[dimension] == category
                        and row["round"] == round_name
                        and row["mode"] == mode
                    ]
                    category_metrics[f"{dimension}/{category}/{round_name}/{mode}"] = {
                        "planned_queries": len(selected),
                        "answerable": sum(row["answerable"] for row in selected),
                        "failed_or_degraded": sum(not row["valid"] for row in selected),
                        "strict_all_queries": {
                            key: sum(row["strict"][key] for row in selected) / len(selected)
                            for key in (
                                "candidate_recall@N",
                                "rerank_pool_recall@M",
                                "recall@5",
                                "mrr@5",
                                "ndcg@5",
                            )
                        },
                    }
    report = {
        "version": "retrieval-v2-compact-v1",
        "synthetic": True,
        "split": raw["split"],
        "started_at": raw["started_at"],
        "finished_at": raw["finished_at"],
        "raw_report_filename": Path(raw_path).name,
        "raw_report_sha256": sha(raw_path),
        "raw_report_bytes": Path(raw_path).stat().st_size,
        "dataset_sha256": raw["dataset_sha256"],
        "code_freeze": raw["code_freeze"],
        "source_unchanged_during_run": raw["source_unchanged_during_run"],
        "run_plan": raw["run_plan"],
        "platform": raw["platform"],
        "versions": raw["versions"],
        "models": raw["models"],
        "indexing": {
            "elapsed_ms": raw["indexing"]["elapsed_ms"],
            "parent_peak_rss_mib": raw["indexing"]["parent_peak_rss_mib"],
        },
        "category_counts": {
            dimension: dict(Counter(query[dimension] for query in queries.values()))
            for dimension in ("language", "kind", "mechanism_group", "answerable")
        },
        "summary": raw["summary"],
        "category_metrics": category_metrics,
        "adoption": raw["adoption"],
        "measurement_limits": raw["measurement_limits"],
        "comparisons": comparisons,
        "privacy": (
            "Only original synthetic IDs, corpus-relative paths and numeric rankings; "
            "no candidate body, credential, real run ID or project path."
        ),
    }
    encoded = json.dumps(report, ensure_ascii=False)
    if "/Users/" in encoded or "/private/" in encoded or "/tmp/" in encoded or "api_key" in encoded:
        raise ValueError("Compact report failed the path/credential-field export check")
    write_new(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compact(args.input, args.output)
    print(json.dumps({"output": str(args.output), "pairs": len(result["comparisons"])}))


if __name__ == "__main__":
    main()
