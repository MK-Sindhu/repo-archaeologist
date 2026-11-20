"""Dense embedders for the hybrid index.

`FastEmbedEmbedder` wraps fastembed (an optional dependency: ``repo-archaeologist[embed]``).
fastembed is imported lazily and the model is only downloaded/loaded on the first `embed`
call, so constructing one is cheap. `get_default_embedder()` returns it when fastembed is
importable and None otherwise (the index then runs BM25-only).

Environment overrides for `get_default_embedder()`:
``ARCH_EMBEDDER=none`` disables dense search; ``ARCH_EMBED_MODEL`` picks another model.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
_DISABLED = frozenset({"none", "off", "0", "false", "bm25"})
LOAD_RETRY_S = 300.0  # after a failed model load, fail fast for this long before retrying


def _text_embedding_cls() -> Any:
    try:
        from fastembed import TextEmbedding
    except ImportError as e:
        raise ImportError(
            "fastembed is not installed; install the 'embed' extra "
            "(pip install 'repo-archaeologist[embed]') or run BM25-only"
        ) from e
    return TextEmbedding


def _as_matrix(vectors: Any, n: int, dim: int) -> np.ndarray:
    mat = np.asarray(list(vectors), dtype=np.float32).reshape(n, dim)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return np.divide(mat, norms, out=np.zeros_like(mat), where=norms > 0)


class FastEmbedEmbedder:
    """`contracts.Embedder` backed by a fastembed ONNX text model.

    `embed` uses the model's passage encoding and `embed_query` its query encoding; both
    return L2-normalised float32 vectors. Texts are encoded in batches of `batch_size`.
    Safe to share between threads (model loading is guarded by a lock; ONNX inference is
    thread-safe).
    """

    def __init__(
        self,
        model: str = DEFAULT_EMBED_MODEL,
        *,
        batch_size: int = 64,
        cache_dir: str | None = None,
        threads: int | None = None,
    ):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.model = model
        self.name = f"fastembed:{model}"
        self.batch_size = batch_size
        self.cache_dir = cache_dir
        self.threads = threads
        self._dim: int | None = None
        self._model: Any = None
        self._lock = threading.Lock()
        self._load_error: BaseException | None = None
        self._load_failed_at = 0.0

    @property
    def dim(self) -> int:
        """Vector size, read from fastembed's model registry (no download)."""
        if self._dim is None:
            self._dim = int(_text_embedding_cls().get_embedding_size(self.model))
        return self._dim

    def _load(self) -> Any:
        """The loaded model. A failed load (no network and no cached model, a corrupt model
        file) is remembered for `LOAD_RETRY_S` seconds: calls in that window fail at once
        instead of each waiting on another download attempt."""
        with self._lock:
            if self._model is None:
                age = time.monotonic() - self._load_failed_at
                if self._load_error is not None and age < LOAD_RETRY_S:
                    err = self._load_error
                    raise RuntimeError(
                        f"embedding model {self.model} failed to load {age:.0f}s ago "
                        f"({type(err).__name__}: {err}); not retrying for "
                        f"{LOAD_RETRY_S - age:.0f}s"
                    ) from err
                logger.info("loading embedding model %s", self.model)
                try:
                    self._model = _text_embedding_cls()(
                        model_name=self.model, cache_dir=self.cache_dir, threads=self.threads
                    )
                except Exception as e:
                    self._load_error, self._load_failed_at = e, time.monotonic()
                    raise
                self._load_error = None
            return self._model

    def embed(self, texts: list[str]) -> np.ndarray:
        """float32 array [len(texts), dim] with L2-normalised rows."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vectors = self._load().passage_embed(list(texts), batch_size=self.batch_size)
        return _as_matrix(vectors, len(texts), self.dim)

    def embed_query(self, text: str) -> np.ndarray:
        """float32 [dim], L2-normalised."""
        return _as_matrix(self._load().query_embed(text), 1, self.dim)[0]


def get_default_embedder(model: str | None = None) -> FastEmbedEmbedder | None:
    """A `FastEmbedEmbedder` if fastembed can be imported, else None (BM25-only).

    `model` defaults to ``$ARCH_EMBED_MODEL`` or BAAI/bge-small-en-v1.5; ``ARCH_EMBEDDER=none``
    forces None. Nothing is downloaded until the embedder is first used.
    """
    if os.getenv("ARCH_EMBEDDER", "").strip().lower() in _DISABLED:
        return None
    try:
        _text_embedding_cls()
    except (ImportError, OSError) as e:
        logger.info("dense embeddings disabled: %s", e)
        return None
    return FastEmbedEmbedder(model or os.getenv("ARCH_EMBED_MODEL") or DEFAULT_EMBED_MODEL)
