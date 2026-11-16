"""Ingest the frozen Markdown corpus and optionally run a literal lexical query."""

import argparse
import json
from pathlib import Path

from incident_desk.knowledge.store import KnowledgeStore

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / ".local/knowledge.db")
    parser.add_argument("--knowledge-dir", type=Path, default=ROOT / "data/knowledge")
    parser.add_argument("--query")
    args = parser.parse_args()
    store = KnowledgeStore(args.db)
    result = {"database": str(args.db), "ingestion": store.ingest_dir(args.knowledge_dir)}
    if args.query is not None:
        result["results"] = store.search(args.query, "checkout-api")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
