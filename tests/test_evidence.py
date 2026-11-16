import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from incident_desk.tools.evidence import EvidenceStore


def fixture_document(incident_id="dev-001"):
    return {
        "incident_id": incident_id,
        "service_id": "checkout-api",
        "version": "fixture-test-v1",
        "window": {"start": "2026-11-09T00:00:00Z", "end": "2026-11-09T01:00:00Z"},
        "evidence": [
            {
                "evidence_id": f"{incident_id}-m-02",
                "source": "metrics",
                "observed_at": "2026-11-09T00:20:00Z",
                "version": "fixture-test-v1",
                "content": "error_rate=0.08",
            },
            {
                "evidence_id": f"{incident_id}-m-01",
                "source": "metrics",
                "observed_at": "2026-11-09T00:10:00Z",
                "version": "fixture-test-v1",
                "content": "error_rate=0.02",
            },
            {
                "evidence_id": f"{incident_id}-l-01",
                "source": "logs",
                "observed_at": "2026-11-09T00:15:00Z",
                "version": "fixture-test-v1",
                "content": "observed pool wait timeout",
            },
        ],
    }


@pytest.fixture
def fixture_root(tmp_path):
    path = tmp_path / "fixtures"
    (path / "dev").mkdir(parents=True)
    (path / "acceptance").mkdir()
    (path / "dev/dev-001.json").write_text(json.dumps(fixture_document()), encoding="utf-8")
    other = fixture_document("dev-002")
    other["evidence"][0]["content"] = "other incident marker"
    (path / "dev/dev-002.json").write_text(json.dumps(other), encoding="utf-8")
    return path


def read(store, **kwargs):
    return store.get(
        **{
            "incident_id": "dev-001",
            "service_id": "checkout-api",
            "start": "2026-11-09T00:00:00Z",
            "end": "2026-11-09T01:00:00Z",
            "source": "metrics",
            "limit": 20,
            **kwargs,
        }
    )


def test_source_bound_sorted_with_stable_provenance(fixture_root):
    store = EvidenceStore(fixture_root)
    result = read(store, limit=1)
    assert [row["evidence_id"] for row in result] == ["dev-001-m-01"]
    assert result[0]["source"] == "metrics"
    assert result[0]["observed_at"] == "2026-11-09T00:10:00Z"
    assert result[0]["version"] == "fixture-test-v1"
    assert read(store, source="logs")[0]["evidence_id"] == "dev-001-l-01"
    assert read(store, source="changes") == []
    assert "other incident marker" not in json.dumps(read(store))
    assert result == read(store, limit=1)


def test_half_open_time_window_and_offset_normalization(fixture_root):
    store = EvidenceStore(fixture_root)
    result = read(store, start="2026-11-09T08:10:00+08:00", end="2026-11-09T08:20:00+08:00")
    assert [row["evidence_id"] for row in result] == ["dev-001-m-01"]
    assert read(store, start="2026-11-09T02:00:00Z", end="2026-11-09T03:00:00Z") == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": "shell"},
        {"service_id": "unrelated"},
        {"limit": 0},
        {"limit": 101},
        {"limit": "2"},
        {"limit": True},
        {"start": "2026-11-09T00:00:00"},
        {"start": "2026-11-09T01:00:00Z"},
        {"end": "2026-11-08T00:00:00Z"},
        {"end": "2026-11-11T00:00:00Z"},
    ],
)
def test_invalid_query_rejected(fixture_root, arguments):
    with pytest.raises(ValidationError):
        read(EvidenceStore(fixture_root), **arguments)


@pytest.mark.parametrize("incident_id", ["../expected/dev-001", "/etc/passwd", "dev-001.json", "x"])
def test_cannot_select_arbitrary_path(fixture_root, incident_id):
    with pytest.raises(ValueError, match="invalid incident"):
        read(EvidenceStore(fixture_root), incident_id=incident_id)


def test_missing_incident_explicit(fixture_root):
    with pytest.raises(KeyError, match="Unknown incident"):
        read(EvidenceStore(fixture_root), incident_id="dev-099")


def test_does_not_open_acceptance_data_during_store_construction(fixture_root, monkeypatch):
    (fixture_root / "acceptance/acc-001.json").write_text("invalid JSON, not opened")
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert "acceptance" not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert read(EvidenceStore(fixture_root))


@pytest.mark.parametrize(
    "mutation,pattern",
    [
        (lambda fixture: fixture.update({"root_cause": "answer leakage"}), "Extra inputs"),
        (lambda fixture: fixture["evidence"].append(fixture["evidence"][0]), "duplicate evidence"),
        (lambda fixture: fixture["evidence"][0].update({"evidence_id": "dev-002-m-01"}), "scoped"),
        (
            lambda fixture: fixture["evidence"][0].update({"observed_at": "2026-11-10T00:10:00Z"}),
            "outside",
        ),
        (lambda fixture: fixture.update({"incident_id": "dev-003"}), "scoped|does not match"),
    ],
)
def test_invalid_fixture_never_returned(fixture_root, mutation, pattern):
    fixture = fixture_document()
    mutation(fixture)
    (fixture_root / "dev/dev-001.json").write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match=pattern):
        read(EvidenceStore(fixture_root))


def test_fixture_symlink_cannot_reveal_answers(tmp_path, fixture_root):
    answer = tmp_path / "expected.json"
    answer.write_text(json.dumps(fixture_document("acc-001")))
    (fixture_root / "acceptance/acc-001.json").symlink_to(answer)
    with pytest.raises(ValueError, match="symlinks cannot escape"):
        EvidenceStore(fixture_root)
