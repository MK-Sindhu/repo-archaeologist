"""Hybrid (BM25 + dense) search over `Chunk`s, stored in one SQLite file.

Tables:

- ``chunks(rid, id UNIQUE, kind, json)``: the chunks; `rid` is a stable integer key shared
  with the FTS table.
- ``chunks_fts``: FTS5 over title and text, after code-aware identifier expansion.
- ``embeddings(id, vector)``: float32 vectors (only when an embedder is configured).
- ``meta(key, value)``: schema version, embedder name/dim, and whatever callers store
  (build.py keeps ``head_sha`` there).

Search fuses the BM25 ranking and the cosine ranking with Reciprocal Rank Fusion (k=60).
Without an embedder the index is BM25-only. All methods are safe to call from several
threads at once (one connection, guarded by a lock).
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import TracebackType
from typing import Literal, Self

import numpy as np

from ..contracts import Embedder
from ..models import Chunk, Hit

logger = logging.getLogger(__name__)

KINDS: tuple[str, ...] = ("code", "doc", "commit")
RRF_K = 60
SCHEMA_VERSION = "1"

_TITLE_WEIGHT = 2.0
_BODY_WEIGHT = 1.0
_MAX_QUERY_TERMS = 64
_MIN_CANDIDATES = 50
_EMBED_BATCH = 256
_RESERVED_META = ("schema_version", "embedder", "embedder_dim")

_WORD = re.compile(r"\w+")
_IDENTIFIER = re.compile(r"[^\W\d]\w*")
_CAMEL_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

# Dropped from queries (when anything else is left): they match nearly every chunk.
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)


class EmbedderMismatchError(RuntimeError):
    """The index was built with a different embedder (name or dimension)."""


# -- tokenisation ----------------------------------------------------------------------------


def split_identifier(token: str) -> list[str]:
    """Lower-cased parts of a snake_case / CamelCase / mixed identifier.

    ``"MAX_BACKOFF" -> ["max", "backoff"]``, ``"HTTPServerError" -> ["http", "server",
    "error"]``, ``"retry" -> ["retry"]``. Non-ASCII pieces are kept whole.
    """
    parts: list[str] = []
    for piece in token.split("_"):
        if not piece:
            continue
        if piece.isascii():
            parts.extend(p.lower() for p in _CAMEL_PART.findall(piece))
        else:
            parts.append(piece.lower())
    return parts


def _expansions(token: str) -> list[str]:
    """Extra index terms for one identifier (its parts, if it has more than one)."""
    parts = split_identifier(token)
    return parts if len(parts) > 1 else []


def expand_identifiers(text: str) -> str:
    """`text` followed by the parts of every compound identifier in it, so that FTS matches
    "max_backoff" against MAX_BACKOFF and "RetryPolicy" against retry_policy (the original
    tokens are kept; the FTS tokenizer treats "_" as part of a word)."""
    extra: list[str] = []
    for m in _IDENTIFIER.finditer(text):
        extra.extend(_expansions(m.group()))
    return f"{text}\n{' '.join(extra)}" if extra else text


def fts_query(text: str, max_terms: int = _MAX_QUERY_TERMS) -> str | None:
    """Build a safe FTS5 MATCH expression from free text, or None if it has no words.

    Word tokens (plus their identifier parts) are double-quoted and joined with OR, so FTS
    operators and syntax in user input (quotes, parentheses, NEAR, AND, *, -, ^, :) are
    matched as plain words or dropped, never interpreted.
    """
    terms: list[str] = []
    seen: set[str] = set()
    for m in _WORD.finditer(text):
        for term in (m.group(), *_expansions(m.group())):
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                terms.append(term)
    content = [t for t in terms if t.casefold() not in _STOPWORDS]
    terms = (content or terms)[:max_terms]
    if not terms:
        return None
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


def embedder_name(embedder: Embedder) -> str:
    """Identity recorded in the index meta: `embedder.name` if set, else the class path."""
    name = getattr(embedder, "name", None)
    return str(name) if name else f"{type(embedder).__module__}.{type(embedder).__qualname__}"


def _embed_text(chunk: Chunk) -> str:
    return chunk.text if chunk.text.startswith(chunk.title) else f"{chunk.title}\n{chunk.text}"


def _normalise(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return np.divide(mat, norms, out=np.zeros_like(mat), where=norms > 0)


# -- index -----------------------------------------------------------------------------------


class HybridIndex:
    """Chunk store with BM25 (FTS5) + dense search fused by Reciprocal Rank Fusion.

    `db_path` is a file path (parent directories are created) or ":memory:".
    `embedder` None means BM25-only. If the file was built with a different embedder,
    `on_embedder_change="error"` (default) raises `EmbedderMismatchError`; "rebuild" drops
    the stored vectors and re-embeds every chunk. Chunks that have no vector yet (added
    while no embedder was configured) are embedded when the index is opened with one, page by
    page; if the embedder fails there (or later, at query time), the index still opens and
    searches with BM25 only.
    """

    def __init__(
        self,
        db_path: str | Path,
        embedder: Embedder | None = None,
        *,
        on_embedder_change: Literal["error", "rebuild"] = "error",
    ):
        if on_embedder_change not in ("error", "rebuild"):
            raise ValueError("on_embedder_change must be 'error' or 'rebuild'")
        self.db_path = str(db_path)
        self.embedder = embedder
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        self._dense_cache: tuple[list[str], np.ndarray, np.ndarray] | None = None
        self._dense_failed = False  # an embedder failure was already logged as a warning
        try:
            self._init_schema()
            self._sync_embedder(on_embedder_change)
        except BaseException:
            self._conn.close()
            raise

    # -- setup -------------------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    rid INTEGER PRIMARY KEY,
                    id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS chunks_kind ON chunks(kind);
                CREATE TABLE IF NOT EXISTS embeddings (
                    id TEXT PRIMARY KEY,
                    vector BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
                    "title, body, tokenize=\"unicode61 remove_diacritics 2 tokenchars '_'\")"
                )
            except sqlite3.OperationalError as e:
                raise RuntimeError(f"SQLite FTS5 is required but unavailable: {e}") from e
            version = self._meta("schema_version")
            if version is None:
                self._put_meta("schema_version", SCHEMA_VERSION)
            elif version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"index {self.db_path} has schema v{version}, expected v{SCHEMA_VERSION}; "
                    "delete the file and rebuild the index"
                )

    def _sync_embedder(self, on_change: str) -> None:
        if self.embedder is None:
            return
        name, dim = embedder_name(self.embedder), int(self.embedder.dim)
        with self._lock:
            stored_name, stored_dim = self._meta("embedder"), self._meta("embedder_dim")
            if stored_name is not None and (stored_name, stored_dim) != (name, str(dim)):
                if on_change == "error":
                    raise EmbedderMismatchError(
                        f"index {self.db_path} was built with embedder {stored_name!r} "
                        f"(dim {stored_dim}) but {name!r} (dim {dim}) was given; open it with "
                        "on_embedder_change='rebuild' to re-embed every chunk, or delete the "
                        "index file"
                    )
                logger.warning(
                    "embedder changed from %s (dim %s) to %s (dim %d): re-embedding %s",
                    stored_name,
                    stored_dim,
                    name,
                    dim,
                    self.db_path,
                )
                with self._conn:
                    self._conn.execute("DELETE FROM embeddings")
            with self._conn:
                self._put_meta("embedder", name)
                self._put_meta("embedder_dim", str(dim))
            self._dense_cache = None
        self._backfill_embeddings()

    def _backfill_embeddings(self) -> int:
        """Embed chunks that have no vector (e.g. added while no embedder was configured).

        Works through the missing chunks one page of `_EMBED_BATCH` at a time (never all of
        them in memory) and logs progress. If the embedder fails (offline with no cached
        model, a corrupt model file, ...) the backfill stops with a warning and the index
        stays usable: chunks without vectors are still found by BM25, and a later open with a
        working embedder resumes the backfill. Returns how many chunks were embedded.
        """
        assert self.embedder is not None
        with self._lock:
            (missing,) = self._conn.execute(
                "SELECT COUNT(*) FROM chunks c LEFT JOIN embeddings e ON e.id = c.id "
                "WHERE e.id IS NULL"
            ).fetchone()
        if not missing:
            return 0
        logger.info("embedding %d chunks without vectors in %s", missing, self.db_path)
        done, last_rid = 0, -1
        while True:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT c.rid, c.json FROM chunks c LEFT JOIN embeddings e ON e.id = c.id "
                    "WHERE e.id IS NULL AND c.rid > ? ORDER BY c.rid LIMIT ?",
                    (last_rid, _EMBED_BATCH),
                ).fetchall()
            if not rows:
                return done
            last_rid = rows[-1][0]
            batch = [Chunk.model_validate_json(r[1]) for r in rows]
            try:
                vecs = self._embed(batch)
            except Exception as e:  # noqa: BLE001 - the index must open; BM25 still works
                logger.warning(
                    "embedding backfill stopped after %d of %d chunks (%s: %s); chunks without "
                    "vectors are searched by BM25 only",
                    done,
                    missing,
                    type(e).__name__,
                    e,
                )
                self._dense_failed = True
                return done
            with self._lock, self._conn:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embeddings(id, vector) "
                    "SELECT ?, ? WHERE EXISTS (SELECT 1 FROM chunks WHERE id = ?)",
                    [(c.id, v.tobytes(), c.id) for c, v in zip(batch, vecs, strict=True)],
                )
                self._dense_cache = None
            done += len(batch)
            if done < missing:
                logger.info("embedded %d/%d chunks", done, missing)

    # -- meta --------------------------------------------------------------------------------

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _put_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_meta(self, key: str) -> str | None:
        """Value stored under `key` in the meta table, or None."""
        with self._lock:
            return self._meta(key)

    def set_meta(self, key: str, value: str | None) -> None:
        """Store `value` under `key` (None deletes it). Reserved keys (schema_version,
        embedder, embedder_dim) are managed by the index and cannot be set."""
        if key in _RESERVED_META:
            raise ValueError(f"meta key {key!r} is managed by the index")
        with self._lock, self._conn:
            self._put_meta(key, value)

    # -- writes ------------------------------------------------------------------------------

    def _embed(self, chunks: Sequence[Chunk]) -> np.ndarray:
        assert self.embedder is not None
        vecs = np.asarray(self.embedder.embed([_embed_text(c) for c in chunks]), dtype=np.float32)
        dim = int(self.embedder.dim)
        if vecs.shape != (len(chunks), dim):
            raise ValueError(
                f"embedder returned shape {vecs.shape}, expected ({len(chunks)}, {dim})"
            )
        return _normalise(vecs)

    def add(self, chunks: Iterable[Chunk]) -> int:
        """Insert or replace (by id) chunks; returns how many were written. Duplicate ids
        within one call keep the last chunk. Embedding happens before the write lock is
        taken, so searches are not blocked by it."""
        batch = list({c.id: c for c in chunks}.values())
        if not batch:
            return 0
        vecs = self._embed(batch) if self.embedder is not None else None
        with self._lock, self._conn:
            for c in batch:
                (rid,) = self._conn.execute(
                    "INSERT INTO chunks(id, kind, json) VALUES (?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET kind = excluded.kind, json = excluded.json "
                    "RETURNING rid",
                    (c.id, c.kind, c.model_dump_json()),
                ).fetchone()
                self._conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (rid,))
                self._conn.execute(
                    "INSERT INTO chunks_fts(rowid, title, body) VALUES (?, ?, ?)",
                    (rid, expand_identifiers(c.title), expand_identifiers(c.text)),
                )
            if vecs is not None:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO embeddings(id, vector) VALUES (?, ?)",
                    [(c.id, v.tobytes()) for c, v in zip(batch, vecs, strict=True)],
                )
            else:  # a vector for the old text would be stale
                self._conn.executemany(
                    "DELETE FROM embeddings WHERE id = ?", [(c.id,) for c in batch]
                )
            self._dense_cache = None
        return len(batch)

    def delete(self, ids: Iterable[str]) -> int:
        """Remove chunks by id; returns how many existed."""
        id_list = list(dict.fromkeys(ids))
        removed = 0
        with self._lock, self._conn:
            for cid in id_list:
                row = self._conn.execute("SELECT rid FROM chunks WHERE id = ?", (cid,)).fetchone()
                if row is None:
                    continue
                self._conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row[0],))
                self._conn.execute("DELETE FROM chunks WHERE rid = ?", (row[0],))
                self._conn.execute("DELETE FROM embeddings WHERE id = ?", (cid,))
                removed += 1
            if removed:
                self._dense_cache = None
        return removed

    def clear(self) -> None:
        """Remove every chunk, vector and caller meta (the embedder identity is kept)."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM chunks_fts")
            self._conn.execute("DELETE FROM chunks")
            self._conn.execute("DELETE FROM embeddings")
            placeholders = ",".join("?" * len(_RESERVED_META))
            self._conn.execute(
                f"DELETE FROM meta WHERE key NOT IN ({placeholders})", _RESERVED_META
            )
            self._dense_cache = None

    # -- reads -------------------------------------------------------------------------------

    def get(self, id: str) -> Chunk | None:
        """The chunk with this id, or None."""
        with self._lock:
            row = self._conn.execute("SELECT json FROM chunks WHERE id = ?", (id,)).fetchone()
        return Chunk.model_validate_json(row[0]) if row else None

    def count(self, kind: str | None = None) -> int:
        """Number of chunks, optionally of one kind."""
        with self._lock:
            if kind is None:
                return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM chunks WHERE kind = ?", (kind,)
            ).fetchone()[0]

    def ids(self, kind: str | None = None) -> set[str]:
        """Ids of all chunks, optionally of one kind."""
        with self._lock:
            if kind is None:
                rows = self._conn.execute("SELECT id FROM chunks").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT id FROM chunks WHERE kind = ?", (kind,)
                ).fetchall()
        return {r[0] for r in rows}

    def ids_by_path(self, kinds: Iterable[str] | str | None = None) -> dict[str, set[str]]:
        """{path: chunk ids} for chunks that have a path (code and doc chunks)."""
        kind_list = self._kinds(kinds)
        sql = "SELECT id, json_extract(json, '$.path') FROM chunks"
        params: tuple[str, ...] = ()
        if kind_list:
            sql += f" WHERE kind IN ({','.join('?' * len(kind_list))})"
            params = kind_list
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: dict[str, set[str]] = {}
        for cid, path in rows:
            if path is not None:
                out.setdefault(path, set()).add(cid)
        return out

    @staticmethod
    def _kinds(kinds: Iterable[str] | str | None) -> tuple[str, ...]:
        if kinds is None:
            return ()
        kind_list = (kinds,) if isinstance(kinds, str) else tuple(dict.fromkeys(kinds))
        unknown = [k for k in kind_list if k not in KINDS]
        if unknown:
            raise ValueError(f"unknown chunk kind(s) {unknown}; expected some of {KINDS}")
        return kind_list

    def search(self, query: str, k: int = 8, kinds: Iterable[str] | str | None = None) -> list[Hit]:
        """Top-`k` chunks for `query`, optionally restricted to `kinds` ("code", "doc",
        "commit").

        Candidates come from BM25 over FTS5 and from cosine similarity over the stored
        vectors; their ranks are fused with Reciprocal Rank Fusion,
        score = sum(1 / (60 + rank)). Hits report each ranking's 1-based rank (None when the
        chunk was not a candidate there). Unknown kinds raise ValueError.
        """
        kind_list = self._kinds(kinds)
        if k <= 0 or (kinds is not None and not kind_list):
            return []
        n_candidates = max(k * 5, _MIN_CANDIDATES)
        qvec = self._query_vector(query)
        bm25_ids = self._bm25(query, kind_list, n_candidates)
        dense_ids = self._dense(qvec, kind_list, n_candidates) if qvec is not None else []

        bm25_rank = {cid: r for r, cid in enumerate(bm25_ids, 1)}
        dense_rank = {cid: r for r, cid in enumerate(dense_ids, 1)}
        scores: dict[str, float] = {}
        for ranks in (bm25_rank, dense_rank):
            for cid, r in ranks.items():
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (RRF_K + r)
        best = {
            cid: min(bm25_rank.get(cid, n_candidates + 1), dense_rank.get(cid, n_candidates + 1))
            for cid in scores
        }
        top = sorted(scores, key=lambda cid: (-scores[cid], best[cid], cid))[:k]
        chunks = self._load(top)
        return [
            Hit(
                chunk=chunks[cid],
                score=scores[cid],
                bm25_rank=bm25_rank.get(cid),
                dense_rank=dense_rank.get(cid),
            )
            for cid in top
            if cid in chunks
        ]

    def _query_vector(self, query: str) -> np.ndarray | None:
        """The query embedding, or None (BM25-only) without an embedder or when it fails.

        Any embedder error (model download/load failures, ONNX runtime errors, which do not
        subclass OSError or RuntimeError) degrades this search to BM25 instead of losing it.
        """
        if self.embedder is None or not query.strip():
            return None
        try:
            return np.asarray(self.embedder.embed_query(query), dtype=np.float32).reshape(-1)
        except Exception as e:  # noqa: BLE001 - dense search is optional; BM25 still answers
            level = logging.DEBUG if self._dense_failed else logging.WARNING
            self._dense_failed = True
            logger.log(level, "query embedding failed (%s: %s); BM25-only", type(e).__name__, e)
            return None

    def _bm25(self, query: str, kinds: tuple[str, ...], limit: int) -> list[str]:
        match = fts_query(query)
        if match is None:
            return []
        sql = (
            "SELECT c.id FROM chunks_fts JOIN chunks c ON c.rid = chunks_fts.rowid "
            "WHERE chunks_fts MATCH ?"
        )
        params: list[object] = [match]
        if kinds:
            sql += f" AND c.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        sql += f" ORDER BY bm25(chunks_fts, {_TITLE_WEIGHT}, {_BODY_WEIGHT}), c.rid LIMIT ?"
        params.append(limit)
        try:
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as e:  # defensive: fts_query only emits quoted terms
            logger.warning("FTS query failed for %r: %s", query, e)
            return []
        return [r[0] for r in rows]

    def _dense_matrix(self) -> tuple[list[str], np.ndarray, np.ndarray]:
        with self._lock:
            if self._dense_cache is None:
                rows = self._conn.execute(
                    "SELECT e.id, c.kind, e.vector FROM embeddings e "
                    "JOIN chunks c ON c.id = e.id ORDER BY c.rid"
                ).fetchall()
                dim = int(self.embedder.dim) if self.embedder is not None else 0
                ids = [r[0] for r in rows]
                kinds = np.array([r[1] for r in rows], dtype=object)
                mat = (
                    np.stack([np.frombuffer(r[2], dtype=np.float32) for r in rows])
                    if rows
                    else np.zeros((0, dim), dtype=np.float32)
                )
                self._dense_cache = (ids, kinds, mat)
            return self._dense_cache

    def _dense(self, qvec: np.ndarray, kinds: tuple[str, ...], limit: int) -> list[str]:
        norm = float(np.linalg.norm(qvec))
        if norm == 0.0:
            return []
        ids, kind_arr, mat = self._dense_matrix()
        if not ids or mat.shape[1] != qvec.shape[0]:
            return []
        idx = np.arange(len(ids))
        if kinds:
            idx = idx[np.isin(kind_arr, kinds)]
        if idx.size == 0:
            return []
        sims = mat[idx] @ (qvec / norm)
        keep = sims > 0.0
        idx, sims = idx[keep], sims[keep]
        if idx.size > limit:
            part = np.argpartition(-sims, limit - 1)[:limit]
            idx, sims = idx[part], sims[part]
        order = np.lexsort((idx, -sims))
        return [ids[i] for i in idx[order]]

    def _load(self, ids: Sequence[str]) -> dict[str, Chunk]:
        if not ids:
            return {}
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, json FROM chunks WHERE id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            ).fetchall()
        return {cid: Chunk.model_validate_json(js) for cid, js in rows}

    # -- lifecycle ---------------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
