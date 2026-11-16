"""Project-scoped immutable knowledge generations with BM25, cosine and RRF."""

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from incident_desk.knowledge.embeddings import EmbeddingUnavailable, LocalEmbeddings
from incident_desk.knowledge.store import tokenize
from incident_desk.projects import (
    PROJECT_ID,
    ProjectNotFound,
    ProjectStore,
    project_path,
    secret_path,
)
from incident_desk.tools.live_evidence import redact

MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = 2000
MAX_SCAN_ENTRIES = 5000
SOURCE_SCAN_SECONDS = 5.0
CHUNK_CHARS = 320
CHUNKER_VERSION = "heading-lines-320-v1"
RRF_K = 60
EXCLUDED = {
    ".git",
    ".local",
    ".venv",
    "node_modules",
    "build",
    "dist",
    "vendor",
    "third_party",
    ".claude",
    ".codex",
    "target",
    "__pycache__",
}


class KnowledgeIndexBusy(RuntimeError):
    """A bounded local installation runs one explicit index operation at a time."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _identity(project_id: str):
    if not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid project knowledge namespace")


def _chunks(project_id: str, path: str, text: str) -> list[dict]:
    version = hashlib.sha256(text.encode()).hexdigest()
    doc_id = digest([project_id, path])[:32]
    chunks, buffer, first, heading = [], "", 1, ""

    def flush(last):
        nonlocal buffer
        if not buffer.strip():
            buffer = ""
            return
        content = (
            heading[:80] + "\n\n" if heading and not buffer.startswith(heading) else ""
        ) + buffer.strip()
        identity = "kn-" + project_id + "-" + digest([doc_id, version, len(chunks), content])[:32]
        chunks.append(
            {
                "id": identity,
                "doc_id": doc_id,
                "version": version,
                "path": path,
                "content": content,
                "line_start": first,
                "line_end": last,
                "terms": tokenize(path + " " + heading + " " + content),
            }
        )
        buffer = ""

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        if line.startswith("#"):
            flush(max(first, line_number - 1))
            heading = line.strip()
        for offset in range(0, max(1, len(line)), CHUNK_CHARS):
            piece = line[offset : offset + CHUNK_CHARS]
            if buffer and len(buffer) + len(piece) > CHUNK_CHARS:
                flush(max(first, line_number - 1 if offset == 0 else line_number))
            if not buffer:
                first = line_number
            buffer += piece
    flush(len(text.splitlines()) or 1)
    return chunks


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("Knowledge documents must be bounded regular text files")
        content = stream.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES or b"\0" in content:
        raise ValueError("Knowledge document exceeds size bounds or is binary")
    return content


def _documents(project) -> tuple[list[dict], list[dict]]:
    files: set[Path] = set()
    scanned = 0
    deadline = time.monotonic() + SOURCE_SCAN_SECONDS
    for name in getattr(project, "knowledge_paths", []):
        logical = Path(name) if Path(name).is_absolute() else project.root / name
        # Resolve only after checking the operator-selected path, including ancestors.
        current = project.root
        for part in logical.relative_to(project.root).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Knowledge sources cannot contain symlinks")
        selected = project_path(project.root, name, must_exist=True)
        if selected.is_file():
            candidates = [selected]
        else:
            candidates = []
            for directory, directories, names in os.walk(selected, followlinks=False):
                if time.monotonic() > deadline:
                    raise ValueError("Knowledge source scan exceeds five seconds")
                scanned += len(names) + len(directories)
                if scanned > MAX_SCAN_ENTRIES:
                    raise ValueError("Knowledge directory scan exceeds 5000 entries")
                directories[:] = sorted(
                    d
                    for d in directories
                    if d not in EXCLUDED
                    and not secret_path(d)
                    and not (Path(directory) / d).is_symlink()
                )
                candidates.extend(
                    Path(directory) / item
                    for item in sorted(names)
                    if Path(item).suffix.lower() in {".md", ".txt"}
                )
        for path in candidates:
            if path.is_symlink() or secret_path(path.relative_to(project.root)):
                continue
            resolved = project_path(project.root, path, must_exist=True)
            if resolved.suffix.lower() not in {".md", ".txt"}:
                raise ValueError("Only Markdown and plain text knowledge sources are supported")
            if any(part in EXCLUDED for part in resolved.relative_to(project.root).parts):
                continue
            files.add(resolved)
            if len(files) > MAX_DOCUMENTS:
                raise ValueError("Knowledge source count exceeds 200 documents")
    documents, chunks, total = [], [], 0
    for path in sorted(files):
        if time.monotonic() > deadline:
            raise ValueError("Knowledge source scan exceeds five seconds")
        raw = _read(path)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Knowledge source bytes exceed 4 MiB")
        text = redact(raw.decode("utf-8"))
        relative = path.relative_to(project.root).as_posix()
        parts = _chunks(project.id, relative, text)
        chunks.extend(parts)
        if len(chunks) > MAX_CHUNKS:
            raise ValueError("Knowledge generation exceeds 2000 chunks")
        documents.append(
            {
                "path": relative,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "redacted_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "chunks": [part["id"] for part in parts],
            }
        )
    return documents, chunks


def _lexical(chunks: list[dict], query: str) -> list[tuple[str, float]]:
    """BM25 statistics are scoped to the frozen generation, independent of other projects."""
    terms = sorted(set(tokenize(query)[:64]))
    frequencies = [Counter(chunk["terms"]) for chunk in chunks]
    average = sum(sum(row.values()) for row in frequencies) / max(1, len(chunks))
    df = {term: sum(term in row for row in frequencies) for term in terms}
    scores = []
    for chunk, counts in zip(chunks, frequencies, strict=True):
        length = sum(counts.values())
        value = 0.0
        for term in terms:
            frequency = counts.get(term, 0)
            if frequency:
                idf = math.log(1 + (len(chunks) - df[term] + 0.5) / (df[term] + 0.5))
                value += (
                    idf
                    * frequency
                    * 2.5
                    / (frequency + 1.5 * (0.25 + 0.75 * length / max(1, average)))
                )
        if value:
            scores.append((chunk["id"], value))
    return sorted(scores, key=lambda item: (-item[1], item[0]))


class ProjectKnowledgeStore:
    def __init__(self, local_dir: str | Path, *, embeddings=None):
        self.local_dir = Path(local_dir)
        self.path = self.local_dir / "project-knowledge.db"
        self.local_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise ValueError("Knowledge database cannot be a symlink")
        self.embeddings = embeddings or LocalEmbeddings(local_dir)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS generations (
                    project_id TEXT NOT NULL, generation TEXT NOT NULL, manifest_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY(project_id,generation));
                CREATE TABLE IF NOT EXISTS active (
                    project_id TEXT PRIMARY KEY,generation TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS generation_chunks (
                    project_id TEXT NOT NULL,generation TEXT NOT NULL,chunk_id TEXT NOT NULL,
                    PRIMARY KEY(project_id,generation,chunk_id),
                    FOREIGN KEY(project_id,generation)
                        REFERENCES generations(project_id,generation),
                    FOREIGN KEY(chunk_id) REFERENCES chunks(id));
                CREATE TABLE IF NOT EXISTS embedding_cache (
                    fingerprint TEXT NOT NULL,text_hash TEXT NOT NULL,vector_json TEXT NOT NULL,
                    PRIMARY KEY(fingerprint,text_hash));
            """)
            connection.commit()
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    def active_generation(self, project_id: str, project_version: int | None = None) -> str | None:
        _identity(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT g.generation,g.manifest_json FROM active a JOIN generations g "
                "ON g.project_id=a.project_id AND g.generation=a.generation "
                "WHERE a.project_id=?",
                (project_id,),
            ).fetchone()
        if not row or (
            project_version is not None
            and json.loads(row["manifest_json"])["project_version"] != project_version
        ):
            return None
        return row["generation"]

    def status(self, project_id: str) -> dict:
        _identity(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT generation,manifest_json,created_at FROM generations "
                "WHERE project_id=? ORDER BY created_at DESC LIMIT 50",
                (project_id,),
            ).fetchall()
        return {
            "project_id": project_id,
            "active_generation": self.active_generation(project_id),
            "generations": [
                {
                    "generation": row["generation"],
                    "created_at": row["created_at"],
                    "project_version": json.loads(row["manifest_json"])["project_version"],
                    "documents": len(json.loads(row["manifest_json"])["documents"]),
                    "chunks": len(json.loads(row["manifest_json"])["chunk_ids"]),
                    "embedding": json.loads(row["manifest_json"])["embedding"],
                }
                for row in rows
            ],
            "model": self.embeddings.status(),
            "limits": {
                "documents": MAX_DOCUMENTS,
                "total_bytes": MAX_TOTAL_BYTES,
                "chunks": MAX_CHUNKS,
                "document_bytes": MAX_DOCUMENT_BYTES,
                "scan_entries": MAX_SCAN_ENTRIES,
                "source_scan_seconds": SOURCE_SCAN_SECONDS,
            },
        }

    def ingest(self, project, *, embed: bool = True) -> dict:
        # Hold the process-safe lock inside the worker, so cancellation of an HTTP
        # coroutine cannot release it while its CPU thread is still indexing.
        path = self.local_dir / "knowledge-index.lock"
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "r+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise KnowledgeIndexBusy("Another knowledge index is already running") from None
            try:
                return self._ingest(project, embed=embed)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _ingest(self, project, *, embed: bool = True) -> dict:
        _identity(project.id)
        started = time.perf_counter()
        documents, chunks = _documents(project)
        model = self.embeddings.status()
        indexed = embed and model["state"] == "ready"
        vectors, reused, encoded, error_code = {}, 0, 0, None
        if indexed:
            missing, hashes = [], []
            with self._connect() as connection:
                for chunk in chunks:
                    text_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()
                    old = connection.execute(
                        "SELECT vector_json FROM embedding_cache "
                        "WHERE fingerprint=? AND text_hash=?",
                        (self.embeddings.fingerprint, text_hash),
                    ).fetchone()
                    if old:
                        vectors[text_hash] = json.loads(old[0])
                        reused += 1
                    elif text_hash not in hashes:
                        hashes.append(text_hash)
                        missing.append(chunk["content"])
            try:
                values = self.embeddings.encode(missing, kind="passage")
                vectors.update(zip(hashes, values, strict=True))
                encoded = len(missing)
            except EmbeddingUnavailable:
                indexed, error_code = False, "embedding_inference_failed"
                vectors = {}
        embedding = {
            "fingerprint": self.embeddings.fingerprint,
            "indexed": indexed,
            "state": "ready" if indexed else error_code or model["state"] if embed else "disabled",
        }
        manifest = {
            "schema": 1,
            "project_id": project.id,
            "project_version": project.version,
            "knowledge_paths": list(getattr(project, "knowledge_paths", [])),
            "documents": documents,
            "chunk_ids": sorted(chunk["id"] for chunk in chunks),
            "chunker": CHUNKER_VERSION,
            "embedding": embedding,
            "lexical": "generation-scoped-bm25-jieba-v1",
            "rrf_k": RRF_K,
        }
        generation = digest(manifest)
        try:
            registered = ProjectStore(self.local_dir).get(project.id)
        except ProjectNotFound:
            registered = None
        if registered is not None and registered.version != project.version:
            raise ValueError("Project registration changed during indexing; reload before indexing")
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            known = (
                connection.execute(
                    "SELECT 1 FROM generations WHERE project_id=? AND generation=?",
                    (project.id, generation),
                ).fetchone()
                is not None
            )
            connection.execute(
                "INSERT OR IGNORE INTO generations VALUES (?,?,?,?)",
                (project.id, generation, canonical(manifest), datetime.now(UTC).isoformat()),
            )
            for chunk in chunks:
                connection.execute(
                    "INSERT OR IGNORE INTO chunks VALUES (?,?,?)",
                    (chunk["id"], project.id, canonical(chunk)),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO generation_chunks VALUES (?,?,?)",
                    (project.id, generation, chunk["id"]),
                )
            for text_hash, vector in vectors.items():
                connection.execute(
                    "INSERT OR IGNORE INTO embedding_cache VALUES (?,?,?)",
                    (self.embeddings.fingerprint, text_hash, canonical(vector)),
                )
            connection.execute(
                "INSERT INTO active VALUES (?,?) ON CONFLICT(project_id) "
                "DO UPDATE SET generation=excluded.generation",
                (project.id, generation),
            )
        return {
            "project_id": project.id,
            "generation": generation,
            "documents": len(documents),
            "chunks": len(chunks),
            "unchanged": known,
            "embedding": embedding,
            "embedding_cache_hits": reused,
            "embedded_chunks": encoded,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    def search(
        self,
        project_id: str,
        generation: str | None,
        query: str,
        mode: str = "hybrid",
        top_k: int = 5,
    ) -> dict:
        _identity(project_id)
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000:
            raise ValueError("Knowledge query must contain 1–1000 characters")
        if mode not in {"lexical", "dense", "hybrid"} or not 1 <= top_k <= 10:
            raise ValueError("Invalid retrieval mode or top_k")
        started = time.perf_counter()
        batch = {
            "status": "complete",
            "items": [],
            "error": None,
            "coverage": {
                "project_id": project_id,
                "index_generation": generation,
                "historical": False,
                "kind": "versioned_project_knowledge",
            },
            "retrieval": {"requested_mode": mode, "effective_mode": mode, "hits": []},
        }
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_json FROM generations WHERE project_id=? AND generation=?",
                (project_id, generation),
            ).fetchone()
            if row is None:
                batch.update(
                    status="unavailable",
                    error={
                        "code": "generation_not_found",
                        "message": "This run has no indexed knowledge generation",
                    },
                )
                return batch
            manifest = json.loads(row[0])
            chunks = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT c.payload_json FROM generation_chunks g JOIN chunks c "
                    "ON c.id=g.chunk_id AND c.project_id=g.project_id "
                    "WHERE g.project_id=? AND g.generation=? ORDER BY c.id",
                    (project_id, generation),
                )
            ]
            lexical = _lexical(chunks, query)
            dense = []
            if mode != "lexical" and chunks:
                try:
                    if (
                        not manifest["embedding"]["indexed"]
                        or manifest["embedding"]["fingerprint"] != self.embeddings.fingerprint
                    ):
                        raise EmbeddingUnavailable("Generation has no matching embedding index")
                    vector = self.embeddings.encode([query], kind="query")[0]
                    for chunk in chunks:
                        text_hash = hashlib.sha256(chunk["content"].encode()).hexdigest()
                        saved = connection.execute(
                            "SELECT vector_json FROM embedding_cache "
                            "WHERE fingerprint=? AND text_hash=?",
                            (self.embeddings.fingerprint, text_hash),
                        ).fetchone()
                        if saved is None:
                            raise EmbeddingUnavailable("Embedding cache is incomplete")
                        values = json.loads(saved[0])
                        dense.append(
                            (chunk["id"], sum(a * b for a, b in zip(vector, values, strict=True)))
                        )
                    dense.sort(key=lambda pair: (-pair[1], pair[0]))
                except (EmbeddingUnavailable, ValueError):
                    batch.update(
                        status="degraded",
                        error={
                            "code": "embedding_unavailable",
                            "message": (
                                "Using lexical retrieval; dense inference is unavailable "
                                "for this generation"
                            ),
                        },
                    )
                    batch["retrieval"]["effective_mode"] = "lexical"
                    dense = []
        effective = batch["retrieval"]["effective_mode"]
        scores = {}
        for label, ranking in (("lexical", lexical), ("dense", dense)):
            for rank, (identity, score) in enumerate(ranking, 1):
                entry = scores.setdefault(identity, {"rrf": 0.0})
                entry[label] = score
                if effective == "hybrid" and rank <= 50:
                    entry["rrf"] += 1 / (RRF_K + rank)
        if effective == "lexical":
            ranked = lexical
        elif effective == "dense":
            ranked = dense
        else:
            ranked = sorted(
                ((identity, row["rrf"]) for identity, row in scores.items() if row["rrf"] > 0),
                key=lambda pair: (-pair[1], pair[0]),
            )
        lookup = {chunk["id"]: chunk for chunk in chunks}
        selected, counts = [], Counter()
        # Prefer document diversity without returning fewer hits for a one-document corpus.
        for identity, _score in ranked:
            if counts[lookup[identity]["doc_id"]] < 2:
                selected.append(identity)
                counts[lookup[identity]["doc_id"]] += 1
                if len(selected) == top_k:
                    break
        selected.extend(identity for identity, _ in ranked if identity not in selected)
        for rank, identity in enumerate(selected[:top_k], 1):
            chunk = lookup[identity]
            batch["items"].append(
                {
                    "id": identity,
                    "evidence_id": identity,
                    "version": chunk["version"],
                    "source": "knowledge",
                    "content": chunk["content"],
                    "provenance": {
                        "project_id": project_id,
                        "generation": generation,
                        "doc_id": chunk["doc_id"],
                        "relative_path": chunk["path"],
                        "line_start": chunk["line_start"],
                        "line_end": chunk["line_end"],
                        "content_sha256": hashlib.sha256(chunk["content"].encode()).hexdigest(),
                        "project_version": manifest["project_version"],
                        "chunker": manifest["chunker"],
                    },
                }
            )
            batch["retrieval"]["hits"].append(
                {"id": identity, "rank": rank, "scores": scores[identity]}
            )
        batch["retrieval"].update(
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            model_fingerprint=manifest["embedding"]["fingerprint"],
            lexical="generation-scoped-bm25-jieba-v1",
            rrf_k=RRF_K,
        )
        batch["coverage"].update(
            candidate_chunks=len(chunks),
            returned_chunks=len(batch["items"]),
            max_results=top_k,
            results_are_guidance_not_runtime_facts=True,
        )
        return batch
