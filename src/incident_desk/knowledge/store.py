"""SQLite FTS5/BM25 retrieval with a single ingestion/query tokenization contract."""

import hashlib
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import jieba
from pydantic import BaseModel, ConfigDict, Field

TOKENIZER_VERSION = "jieba-cut-for-search-hmm-false-nfkc-v1"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
TOKEN_PART = re.compile(r"\w+", re.UNICODE)
_tokenizer = jieba.Tokenizer()


class KnowledgeSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    service_id: Literal["checkout-api"]
    top_k: Annotated[int, Field(ge=1, le=10, strict=True)] = 5


def tokenize(text: str) -> list[str]:
    """Apply identical normalization and jieba search segmentation in both paths."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = []
    for token in _tokenizer.cut_for_search(normalized, HMM=False):
        tokens.extend(TOKEN_PART.findall(token))
    return tokens


def match_query(text: str) -> str:
    """Treat all user input as literal terms, never as user-supplied FTS grammar."""
    terms = list(dict.fromkeys(tokenize(text)))[:64]
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def _document(path: Path, source: str) -> dict:
    raw = path.read_text(encoding="utf-8")
    metadata = re.match(r"^---\n(.*?)\n---\n", raw, flags=re.S)
    if metadata is None:
        raise ValueError(f"Missing knowledge metadata: {source}")
    fields = {}
    for line in metadata.group(1).splitlines():
        key, delimiter, value = line.partition(":")
        if not delimiter or key.strip() in fields:
            raise ValueError(f"Invalid/duplicate metadata: {source}")
        fields[key.strip()] = value.strip()
    if set(fields) != {"doc_id", "doc_version", "service_id"}:
        raise ValueError(f"Knowledge metadata requires doc_id/doc_version/service_id: {source}")
    if not all(IDENTIFIER.fullmatch(value) for value in fields.values()):
        raise ValueError(f"Invalid knowledge identifier: {source}")
    chunks = []
    headings: list[tuple[int, str]] = []
    body = raw[metadata.end() :]
    for paragraph in re.split(r"\n\s*\n", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # Heading-only paragraphs provide context; mixed heading/body paragraphs
        # remain intact as content rather than silently dropping prose.
        heading = re.fullmatch(r"(#{1,6}) +([^\n]+)", paragraph)
        if heading:
            depth = len(heading.group(1))
            headings = [(level, title) for level, title in headings if level < depth]
            headings.append((depth, paragraph))
            continue
        prefix = "\n".join(title for _, title in headings)
        for offset in range(0, len(paragraph), 1600):
            content = ((prefix + "\n\n") if prefix else "") + paragraph[offset : offset + 1600]
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:20]
            chunk_id = f"{fields['doc_id']}@{fields['doc_version']}:{digest}"
            if any(chunk["chunk_id"] == chunk_id for chunk in chunks):
                continue
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "content": content,
                    "tokenized": " ".join(tokenize(content)),
                    "source": f"{source}#paragraph-{len(chunks) + 1}",
                }
            )
    if not chunks:
        raise ValueError(f"Knowledge document has no content: {source}")
    return {
        **fields,
        "source": source,
        "content_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "chunks": chunks,
    }


class KnowledgeStore:
    """Append-only document/chunk versions, with one current version per document.

    A new version activates when first ingested. Re-ingesting an already-known
    version never rolls the active pointer back. Existing chunks remain available
    through get_chunk, allowing old evidence snapshots to resolve after updates.
    """

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS knowledge_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    doc_id TEXT NOT NULL, doc_version TEXT NOT NULL,
                    service_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    source TEXT NOT NULL, ingested_at TEXT NOT NULL,
                    PRIMARY KEY (doc_id, doc_version)
                );
                CREATE TABLE IF NOT EXISTS active_documents (
                    doc_id TEXT PRIMARY KEY, doc_version TEXT NOT NULL,
                    FOREIGN KEY (doc_id, doc_version) REFERENCES documents(doc_id, doc_version)
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    rowid INTEGER PRIMARY KEY,
                    chunk_id TEXT NOT NULL UNIQUE, doc_id TEXT NOT NULL,
                    doc_version TEXT NOT NULL, service_id TEXT NOT NULL,
                    content TEXT NOT NULL, tokenized TEXT NOT NULL, source TEXT NOT NULL,
                    FOREIGN KEY (doc_id, doc_version) REFERENCES documents(doc_id, doc_version)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    tokenized, content='chunks', content_rowid='rowid', tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, tokenized) VALUES (new.rowid, new.tokenized);
                END;
            """)
            row = connection.execute(
                "SELECT value FROM knowledge_metadata WHERE key = 'tokenizer_version'"
            ).fetchone()
            if row is not None and row[0] != TOKENIZER_VERSION:
                raise ValueError("Knowledge tokenizer version differs; create a new index")
            connection.execute(
                "INSERT OR IGNORE INTO knowledge_metadata VALUES ('tokenizer_version', ?)",
                (TOKENIZER_VERSION,),
            )
            connection.commit()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def ingest_dir(self, directory: str | Path) -> dict:
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError("Knowledge directory does not exist")
        paths = sorted(root.rglob("*.md"))
        documents = []
        for path in paths:
            if not path.resolve().is_relative_to(root):
                raise ValueError("Knowledge symlinks cannot escape the source directory")
            documents.append(_document(path, path.relative_to(root).as_posix()))
        inserted_documents = inserted_chunks = unchanged = 0
        with self._connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for document in documents:
                identity = (document["doc_id"], document["doc_version"])
                old = connection.execute(
                    "SELECT content_hash FROM documents WHERE doc_id=? AND doc_version=?", identity
                ).fetchone()
                if old is not None:
                    if old[0] != document["content_hash"]:
                        raise ValueError("Document content changed without a new doc_version")
                    unchanged += 1
                    continue
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        *identity,
                        document["service_id"],
                        document["content_hash"],
                        document["source"],
                        datetime.now(UTC).isoformat(),
                    ),
                )
                for chunk in document["chunks"]:
                    connection.execute(
                        """
                        INSERT INTO chunks
                        (chunk_id, doc_id, doc_version, service_id, content, tokenized, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            chunk["chunk_id"],
                            *identity,
                            document["service_id"],
                            chunk["content"],
                            chunk["tokenized"],
                            chunk["source"],
                        ),
                    )
                    inserted_chunks += 1
                connection.execute(
                    """
                    INSERT INTO active_documents VALUES (?, ?)
                    ON CONFLICT(doc_id) DO UPDATE SET doc_version=excluded.doc_version
                """,
                    identity,
                )
                inserted_documents += 1
            totals = dict(
                connection.execute("""
                SELECT (SELECT count(*) FROM documents) AS total_versions,
                       (SELECT count(*) FROM active_documents) AS active_documents,
                       (SELECT count(*) FROM chunks) AS total_chunks
            """).fetchone()
            )
        return {
            "inserted_documents": inserted_documents,
            "inserted_chunks": inserted_chunks,
            "unchanged_documents": unchanged,
            **totals,
            "tokenizer_version": TOKENIZER_VERSION,
        }

    def search(self, query: str, service_id: str, top_k: int = 5) -> list[dict]:
        request = KnowledgeSearch(query=query, service_id=service_id, top_k=top_k)
        expression = match_query(request.query)
        if not expression:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.chunk_id, c.doc_id, c.doc_version, c.service_id, c.content, c.source,
                       bm25(chunks_fts) AS bm25
                FROM chunks_fts
                JOIN chunks c ON c.rowid=chunks_fts.rowid
                JOIN active_documents a ON a.doc_id=c.doc_id AND a.doc_version=c.doc_version
                WHERE chunks_fts MATCH ? AND c.service_id=?
                ORDER BY bm25 ASC, c.chunk_id ASC
                LIMIT ?
            """,
                (expression, request.service_id, request.top_k),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_chunk(self, chunk_id: str) -> dict | None:
        if not isinstance(chunk_id, str) or len(chunk_id) > 200:
            raise ValueError("Invalid chunk ID")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT chunk_id, doc_id, doc_version, service_id, content, source
                FROM chunks WHERE chunk_id=?
            """,
                (chunk_id,),
            ).fetchone()
        return None if row is None else dict(row)
