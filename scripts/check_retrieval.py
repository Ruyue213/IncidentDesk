"""Measure CP03 lexical retrieval on development questions, never acceptance labels."""

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from incident_desk.knowledge.store import KnowledgeStore
from incident_desk.tools.evidence import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    cases = []
    fixture_report = []
    with tempfile.TemporaryDirectory(prefix="incident-desk-retrieval-") as temporary:
        store = KnowledgeStore(Path(temporary) / "knowledge.db")
        first = store.ingest_dir(ROOT / "data/knowledge")
        repeated = store.ingest_dir(ROOT / "data/knowledge")
        assert repeated["inserted_chunks"] == repeated["inserted_documents"] == 0
        evidence = EvidenceStore(ROOT / "data/fixtures")
        matched = expected_total = 0
        for path in sorted((ROOT / "evals/dev").glob("*.json")):
            given = json.loads(path.read_text(encoding="utf-8"))
            # Only dev IDs are selected here. No acceptance expected file is opened.
            assert given["incident_id"].startswith("dev-")
            expected = json.loads((ROOT / "evals/expected" / path.name).read_text())
            results = store.search(given["question"], given["service_id"])
            expected_docs = set(expected["expected_doc_ids"])
            actual_docs = {result["doc_id"] for result in results}
            found = expected_docs & actual_docs
            matched += len(found)
            expected_total += len(expected_docs)
            cases.append(
                {
                    "incident_id": given["incident_id"],
                    "query": given["question"],
                    "expected_doc_ids": sorted(expected_docs),
                    "retrieved": results,
                    "matched_doc_ids": sorted(found),
                    "document_recall_at_5": {
                        "numerator": len(found),
                        "denominator": len(expected_docs),
                    },
                }
            )
            for source in ("metrics", "logs", "changes"):
                rows = evidence.get(
                    given["incident_id"],
                    given["service_id"],
                    given["window"]["start"],
                    given["window"]["end"],
                    source,
                )
                assert all(
                    row["evidence_id"].startswith(given["incident_id"] + "-") for row in rows
                )
                fixture_report.append(
                    {
                        "incident_id": given["incident_id"],
                        "source": source,
                        "evidence_ids": [row["evidence_id"] for row in rows],
                        "count": len(rows),
                    }
                )
        focused = store.search("连接池等待超时", "checkout-api")
        assert "kb-002" in {row["doc_id"] for row in focused}
        report = {
            "checked_at": datetime.now(UTC).isoformat(),
            "split": "dev",
            "case_count": len(cases),
            "dataset_manifest_sha256": hashlib.sha256(
                (ROOT / "evals/dataset-manifest.json").read_bytes()
            ).hexdigest(),
            "ingestion_first": first,
            "ingestion_repeated": repeated,
            "metric": "Expected-document recall among the top 5 chunks for each original question",
            "micro_document_recall_at_5": {"numerator": matched, "denominator": expected_total},
            "cases": cases,
            "focused_chinese_query": {"query": "连接池等待超时", "results": focused},
            "development_fixture_queries": fixture_report,
            "limitations": [
                "Lexical recall is distinct from diagnostic correctness and citation support.",
                "Generic questions may not name the relevant mechanism; "
                "do not infer missing terms.",
                "Acceptance inputs/expected answers were not used for this retrieval measurement.",
                "The 8 development cases are small synthetic examples, "
                "not an industrial accuracy estimate.",
            ],
            "model_api_calls": 0,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
