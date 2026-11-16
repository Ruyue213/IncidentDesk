"""Freeze CP03 materials once, or verify that the predefined corpus has not changed.

This does not claim independent blind testing or diagnostic correctness. The
manifest covers inputs, observation fixtures, knowledge, labels and rubric.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = Path("evals/dataset-manifest.json")
PATTERNS = (
    "data/knowledge/*.md",
    "data/fixtures/dev/*.json",
    "data/fixtures/acceptance/*.json",
    "evals/dev/*.json",
    "evals/acceptance/*.json",
    "evals/expected/*.json",
    "evals/rubric.json",
    "evals/README.md",
)
FIXTURE_KEYS = {"incident_id", "service_id", "version", "window", "evidence"}
EVIDENCE_KEYS = {"evidence_id", "source", "observed_at", "version", "content"}
INPUT_KEYS = {"incident_id", "service_id", "window", "question"}


def aware_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("All dataset times require a timezone")
    return result


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(root: Path) -> dict:
    """Validate boundaries and reference integrity without invoking any model."""
    doc_ids = set()
    knowledge_files = sorted((root / "data/knowledge").glob("*.md"))
    if not 6 <= len(knowledge_files) <= 10:
        raise ValueError("Knowledge corpus requires 6–10 Markdown documents")
    for path in knowledge_files:
        frontmatter = re.match(r"^---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
        if frontmatter is None:
            raise ValueError(f"Missing frontmatter: {path.name}")
        fields = dict(line.split(": ", 1) for line in frontmatter.group(1).splitlines())
        if set(fields) != {"doc_id", "doc_version", "service_id"}:
            raise ValueError(f"Unexpected knowledge metadata: {path.name}")
        if fields["doc_id"] in doc_ids or fields["service_id"] != "checkout-api":
            raise ValueError(f"Duplicate document or service mismatch: {path.name}")
        doc_ids.add(fields["doc_id"])

    observed_ids = set()
    incident_ids = set()
    groups: dict[str, set[str]] = {"dev": set(), "acceptance": set()}
    families: dict[str, set[str]] = {"dev": set(), "acceptance": set()}
    for split, prefix, size in (("dev", "dev", 8), ("acceptance", "acc", 12)):
        fixtures = sorted((root / "data/fixtures" / split).glob("*.json"))
        inputs = sorted((root / "evals" / split).glob("*.json"))
        if len(fixtures) != size or len(inputs) != size:
            raise ValueError(f"Expected {size} complete fixtures and inputs in {split}")
        for fixture_path in fixtures:
            incident_id = fixture_path.stem
            if not re.fullmatch(rf"{prefix}-\d{{3}}", incident_id):
                raise ValueError(f"Fixture filename must not disclose labels: {fixture_path.name}")
            fixture = load_json(fixture_path)
            given = load_json(root / "evals" / split / fixture_path.name)
            expected = load_json(root / "evals/expected" / fixture_path.name)
            if set(fixture) != FIXTURE_KEYS or set(given) != INPUT_KEYS:
                raise ValueError(
                    f"Labels or unexpected fields in tool/input material: {incident_id}"
                )
            if fixture["incident_id"] != incident_id or given["incident_id"] != incident_id:
                raise ValueError(f"Incident identifier mismatch: {incident_id}")
            if expected["incident_id"] != incident_id or incident_id in incident_ids:
                raise ValueError(f"Duplicate or mismatched annotation: {incident_id}")
            incident_ids.add(incident_id)
            groups[split].add(expected["incident_group"])
            families[split].add(expected["fault_family"])
            if fixture["service_id"] != "checkout-api" or given["service_id"] != "checkout-api":
                raise ValueError(f"Service boundary mismatch: {incident_id}")
            if given["window"] != fixture["window"]:
                raise ValueError(f"Input and fixture windows differ: {incident_id}")
            if set(fixture["window"]) != {"start", "end"}:
                raise ValueError(f"Unexpected window shape: {incident_id}")
            start = aware_time(fixture["window"]["start"])
            end = aware_time(fixture["window"]["end"])
            if start >= end:
                raise ValueError(f"Window must have positive duration: {incident_id}")
            current_ids = set()
            for item in fixture["evidence"]:
                if set(item) != EVIDENCE_KEYS:
                    raise ValueError(f"Unexpected evidence fields: {incident_id}")
                evidence_id = item["evidence_id"]
                if evidence_id in observed_ids or not evidence_id.startswith(incident_id + "-"):
                    raise ValueError(f"Evidence ID is not unique/scoped: {evidence_id}")
                observed_ids.add(evidence_id)
                current_ids.add(evidence_id)
                if item["source"] not in {"metrics", "logs", "changes"}:
                    raise ValueError(f"Unknown source: {evidence_id}")
                if not start <= aware_time(item["observed_at"]) < end:
                    raise ValueError(f"Evidence outside input time window: {evidence_id}")
                if item["version"] != fixture["version"] or not isinstance(item["content"], str):
                    raise ValueError(f"Invalid evidence content or version: {evidence_id}")
            for key in (
                "expected_evidence_ids",
                "supporting_evidence_ids",
                "contradicting_evidence_ids",
            ):
                if not set(expected[key]) <= current_ids:
                    raise ValueError(f"Unresolvable annotation {key}: {incident_id}")
            if not set(expected["expected_doc_ids"]) <= doc_ids:
                raise ValueError(f"Unresolvable expected knowledge document: {incident_id}")
            for key in ("acceptable_conclusions", "recommended_checks", "scoring_notes"):
                if not expected[key]:
                    raise ValueError(f"Incomplete annotation {key}: {incident_id}")

    if groups["dev"] & groups["acceptance"]:
        raise ValueError("The same incident group must not cross data splits")
    if len(list((root / "evals/expected").glob("*.json"))) != 20:
        raise ValueError("Expected exactly 20 complete annotations")
    required_families = {
        "connection_pool",
        "downstream_latency",
        "dependency_rate_limit",
        "normal",
        "insufficient_evidence",
        "conflicting_evidence",
    }
    for split in groups:
        if not required_families <= families[split]:
            raise ValueError(f"Missing required mechanism/behavior coverage in {split}")
    rubric = load_json(root / "evals/rubric.json")
    if rubric["dataset_counts"] != {"dev": 8, "acceptance": 12}:
        raise ValueError("Rubric counts disagree with the frozen dataset")
    return {
        "knowledge_documents": len(doc_ids),
        "dev_incidents": 8,
        "acceptance_incidents": 12,
        "evidence_records": len(observed_ids),
        "incident_groups_crossing_splits": 0,
        "reference_integrity": "passed",
        "required_families_in_both_splits": "passed",
    }


def material_hashes(root: Path) -> dict[str, str]:
    paths = sorted({path for pattern in PATTERNS for path in root.glob(pattern)})
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify, never rewrite, the frozen manifest"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / MANIFEST
    try:
        validation = validate(root)
        current_hashes = material_hashes(root)
        if args.check:
            manifest = load_json(manifest_path)
            frozen_hashes = manifest["files"]
            if current_hashes != frozen_hashes:
                names = sorted(set(current_hashes) | set(frozen_hashes))
                changed = [
                    name for name in names if current_hashes.get(name) != frozen_hashes.get(name)
                ]
                raise ValueError(f"Frozen dataset differs: {', '.join(changed)}")
            result = "verified_unchanged"
        else:
            if manifest_path.exists():
                raise ValueError("Manifest already exists; refusing to overwrite frozen materials")
            manifest = {
                "version": "incidentdesk-dataset-v1",
                "fixture_version": "fixtures-v1",
                "rubric_version": "rubric-v1",
                "frozen_at": datetime.now(UTC).isoformat(),
                "frozen_before_retrieval_implementation": True,
                "split": {"dev": 8, "acceptance": 12},
                "split_unit": (
                    "separately numbered synthetic incident group; all variants remain in one split"
                ),
                "independent_blind_held_out": False,
                "limitation": (
                    "Authored by an agent participating in development; predefined acceptance only"
                ),
                "cp04_to_cp09_allowed_split": "dev",
                "validation_at_freeze": validation,
                "files": current_hashes,
            }
            with manifest_path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            result = "frozen"
        print(
            json.dumps(
                {
                    "result": result,
                    "manifest": MANIFEST.as_posix(),
                    "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                    "material_files": len(current_hashes),
                    "validation": validation,
                    "model_api_calls": 0,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (ValueError, KeyError, FileNotFoundError, TypeError) as exc:
        print(
            json.dumps({"result": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
