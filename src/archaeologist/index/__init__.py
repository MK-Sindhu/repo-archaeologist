"""Local hybrid (BM25 + dense) index over code, docs and commit messages."""

from .build import build_index
from .embed import FastEmbedEmbedder, get_default_embedder
from .store import EmbedderMismatchError, HybridIndex

__all__ = [
    "EmbedderMismatchError",
    "FastEmbedEmbedder",
    "HybridIndex",
    "build_index",
    "get_default_embedder",
]
