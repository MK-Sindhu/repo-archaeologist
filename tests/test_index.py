"""Tests for archaeologist.index: chunkers, hybrid store, embedders and the index builder."""

from __future__ import annotations

import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np
import pytest
from conftest import README_V2, RETRY_V3

from archaeologist.contracts import Embedder
from archaeologist.index import embed as embed_mod
from archaeologist.index.build import build_index, is_excluded, is_included
from archaeologist.index.chunkers import (
    chunk_file,
    chunk_markdown,
    chunk_python,
    chunk_rst,
    chunk_text,
    commit_chunk,
)
from archaeologist.index.embed import FastEmbedEmbedder, get_default_embedder
from archaeologist.index.store import (
    RRF_K,
    EmbedderMismatchError,
    HybridIndex,
    expand_identifiers,
    fts_query,
    split_identifier,
)
from archaeologist.models import Chunk
from archaeologist.refs import code_ref, commit_ref, doc_ref
from archaeologist.testing import HashEmbedder

RETRY_PATH = "src/pkg/retry.py"


def by_symbol(chunks: list[Chunk]) -> dict[str | None, Chunk]:
    return {c.symbol: c for c in chunks}


def mk(id: str, text: str, kind: str = "code", title: str | None = None, **kw) -> Chunk:
    return Chunk(id=id, kind=kind, title=title if title is not None else id, text=text, **kw)


# -- chunkers --------------------------------------------------------------------------------


def test_chunk_python_functions_class_methods_and_module():
    chunks = chunk_python(RETRY_PATH, RETRY_V3)
    syms = by_symbol(chunks)
    assert set(syms) == {None, "backoff", "Retrier", "Retrier.__init__", "Retrier.run"}

    run = syms["Retrier.run"]
    assert run.id == code_ref(RETRY_PATH, 20, 26)
    assert (run.kind, run.start_line, run.end_line) == ("code", 20, 26)
    assert run.text.splitlines()[0] == f"{RETRY_PATH}:20-26 Retrier.run"
    assert "return fn()" in run.text
    assert run.meta["node"] == "method"

    backoff = syms["backoff"]
    assert backoff.id == code_ref(RETRY_PATH, 8, 13)
    assert backoff.text.splitlines()[0] == f"{RETRY_PATH}:8-13 backoff"
    assert "random.uniform(0, delay)" in backoff.text

    cls = syms["Retrier"]
    assert cls.id == code_ref(RETRY_PATH, 16, 26)
    assert "def __init__(self, attempts=3):" in cls.text
    assert "def run(self, fn):" in cls.text
    assert "return fn()" not in cls.text  # method bodies live in their own chunks

    module = syms[None]
    assert module.meta["node"] == "module"
    assert module.id == code_ref(RETRY_PATH, 1, 5)
    assert '"""Retry helpers."""' in module.text and "MAX_BACKOFF = 30" in module.text
    assert "def backoff" not in module.text
    assert [c.start_line for c in chunks] == sorted(c.start_line for c in chunks)


def test_chunk_python_decorators_comments_docstrings_nested_async():
    src = '''import functools

# Why: callers rely on the cache being per-process.
# See the design notes.
@functools.cache
def cached(x):
    return x


class Outer:
    """Outer docs.

    More detail."""

    LIMIT = 3

    @property
    def size(self):
        """Size of the thing.

        Long explanation."""
        return 1

    async def fetch(self, url):
        return url

    class Inner:
        def deep(self):
            return 2
'''
    syms = by_symbol(chunk_python("m.py", src))
    assert {
        "cached",
        "Outer",
        "Outer.size",
        "Outer.fetch",
        "Outer.Inner",
        "Outer.Inner.deep",
    } <= set(syms)
    cached = syms["cached"]
    assert cached.start_line == 3  # comment block + decorator included
    assert "# Why: callers rely" in cached.text and "@functools.cache" in cached.text
    outer = syms["Outer"].text
    assert '"""Outer docs.' in outer and "LIMIT = 3" in outer
    assert "@property" in outer and '"""Size of the thing."""' in outer
    assert "Long explanation" not in outer and "return 1" not in outer
    assert "async def fetch(self, url):" in outer
    assert syms["Outer.fetch"].meta["node"] == "method"
    assert "return 2" in syms["Outer.Inner.deep"].text
    assert syms["Outer.size"].text.splitlines()[0] == "m.py:17-22 Outer.size"


def test_chunk_python_comment_inside_previous_string_is_not_attached():
    src = 'X = """\n# not a comment"""\ndef f():\n    return X\n'
    syms = by_symbol(chunk_python("s.py", src))
    assert syms["f"].start_line == 3


def test_chunk_python_syntax_error_falls_back_to_text():
    chunks = chunk_python("bad.py", "def broken(:\n    pass\n")
    assert len(chunks) == 1
    assert chunks[0].id == code_ref("bad.py", 1, 2)
    assert chunks[0].meta["node"] == "window" and chunks[0].symbol is None


def test_chunk_python_long_definition_is_windowed_and_keeps_symbol():
    body = "\n".join(f"    x{i} = {i}" for i in range(150))
    chunks = chunk_python("long.py", f"def big():\n{body}\n    return 0\n", max_lines=100)
    assert len(chunks) > 1
    assert all(c.symbol == "big" for c in chunks)
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 152
    assert chunks[0].meta["parts"] == len(chunks)


def test_chunk_text_windows_overlap_and_validation():
    src = "\n".join(f"line {i}" for i in range(1, 131)) + "\n"
    chunks = chunk_text("a.js", src)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 60), (51, 110), (101, 130)]
    assert chunks[0].id == code_ref("a.js", 1, 60)
    assert chunks[0].text.splitlines()[0] == "a.js:1-60"
    assert chunks[1].title == "a.js (lines 51-110)"
    assert chunk_text("blank.txt", "\n\n   \n") == []
    assert chunk_text("e.js", "") == []
    assert chunk_text("n.txt", "notes", kind="doc")[0].kind == "doc"
    with pytest.raises(ValueError):
        chunk_text("a.js", src, window=10, overlap=10)


def test_chunk_markdown_sections_on_readme():
    chunks = chunk_markdown("README.md", README_V2)
    assert [c.id for c in chunks] == [
        doc_ref("README.md", "toy"),
        doc_ref("README.md", "retries"),
        doc_ref("README.md", "settings"),
    ]
    retries = chunks[1]
    assert retries.kind == "doc" and retries.title == "Retries"
    assert retries.text.splitlines()[0] == "README.md > toy > Retries"
    assert "capped at 30 seconds" in retries.text
    assert "MAX_BACKOFF" not in retries.text
    assert (retries.start_line, retries.end_line) == (5, 8)


def test_chunk_markdown_duplicates_fences_setext_front_matter_preamble():
    src = """---
title: Front matter
---
Intro before any heading.

## Usage
first
## Usage
second
```python
# not a heading
```
## Usage 2
real heading whose slug collides
## Usage
third
Setext Title
============
body
### [Linked](http://x) heading {#custom-id}
"""
    ids = [c.id.split("#", 1)[1] for c in chunk_markdown("d.md", src)]
    assert ids == ["top", "usage", "usage-2", "usage-2-2", "usage-3", "setext-title", "custom-id"]
    chunks = {c.id: c for c in chunk_markdown("d.md", src)}
    assert "# not a heading" in chunks["doc:d.md#usage-2"].text
    assert "Intro before any heading." in chunks["doc:d.md#top"].text
    assert chunks["doc:d.md#custom-id"].title == "Linked heading"


def test_chunk_markdown_list_items_and_rules_are_not_setext_headings():
    src = "# T\n\n1. first step\n---\n- bullet\n---\n\n---\ntext\n"
    assert [c.id for c in chunk_markdown("l.md", src)] == ["doc:l.md#t"]


def test_chunk_rst_sections():
    src = "=====\nGuide\n=====\n\nIntro.\n\nInstall\n-------\n\npip install\n\nInstall\n-------\n"
    chunks = chunk_rst("docs/guide.rst", src)
    assert [c.id for c in chunks] == [
        "doc:docs/guide.rst#guide",
        "doc:docs/guide.rst#install",
        "doc:docs/guide.rst#install-2",
    ]
    assert [c.meta["level"] for c in chunks] == [1, 2, 2]
    assert "pip install" in chunks[1].text


@dataclass
class FakeCommit:
    sha: str
    subject: str
    body: str = ""
    files: list[str] = field(default_factory=list)
    author: str = "Dev"
    date: str = "2024-02-15T12:00:00+00:00"

    @property
    def short_sha(self) -> str:
        return self.sha[:7]


def test_commit_chunk():
    c = FakeCommit(
        "a" * 40,
        "Cap backoff at 30s to avoid huge sleeps (#12)",
        "Fixes #7. Users reported multi-minute sleeps.",
        ["src/pkg/retry.py"],
    )
    chunk = commit_chunk(c)
    assert chunk.id == commit_ref("a" * 40) and chunk.kind == "commit"
    assert chunk.title == c.subject and chunk.date == c.date
    assert "multi-minute sleeps" in chunk.text and "src/pkg/retry.py" in chunk.text
    assert chunk.meta["refs"] == ["pr:12", "issue:7"]
    many = commit_chunk(FakeCommit("b" * 40, "mass rename", files=[f"f{i}" for i in range(80)]))
    assert "(+30 more files)" in many.text


def test_chunk_file_dispatch():
    assert chunk_file("x.py", "def f():\n    pass\n")[0].symbol == "f"
    assert chunk_file("README.md", "# Hi\n")[0].kind == "doc"
    assert chunk_file("g.rst", "Hi\n==\n\ntext\n")[0].kind == "doc"
    assert chunk_file("notes.txt", "hello\n")[0].kind == "doc"
    assert chunk_file("main.go", "package main\n")[0].kind == "code"


# -- store: tokenisation and query safety ----------------------------------------------------


def test_split_identifier_and_expansion():
    assert split_identifier("MAX_BACKOFF") == ["max", "backoff"]
    assert split_identifier("RetryPolicy") == ["retry", "policy"]
    assert split_identifier("HTTPServerError") == ["http", "server", "error"]
    assert split_identifier("getHTTP2Response") == ["get", "http", "2", "response"]
    assert split_identifier("_private") == ["private"]
    expanded = expand_identifiers("x = retry_policy(MaxBackoff)")
    assert expanded.startswith("x = retry_policy(MaxBackoff)")
    assert expanded.split("\n")[1].split() == ["retry", "policy", "max", "backoff"]


HOSTILE = [
    'backoff" OR "x',
    '"',
    '""',
    "(backoff",
    "backoff)",
    "NEAR(backoff retry, 2)",
    "backoff NEAR retry",
    "back*",
    "*",
    "-backoff",
    "backoff AND OR NOT",
    "title:backoff",
    "^backoff",
    "{backoff}",
    "'single' quotes",
    "naïve résumé 日本語 🙂 ǅ",
    "\x00\x01 backoff",
    "\\",
    "",
    "   ",
    "é",
    "‿",
]


@pytest.mark.parametrize("query", HOSTILE)
def test_fts_escaping_hostile_input(tmp_path, query):
    index = HybridIndex(tmp_path / "i.db")
    index.add([mk("code:a.py#L1-L2", "def backoff(): retry naïve")])
    match = fts_query(query)
    if match is not None:
        assert all(t.startswith('"') and t.endswith('"') for t in match.split(" OR "))
    hits = index.search(query)  # must not raise
    assert all(h.chunk.id == "code:a.py#L1-L2" for h in hits)


def test_fts_query_drops_stopwords_but_keeps_something():
    assert fts_query("why is the backoff capped?") == '"backoff" OR "capped"'
    assert fts_query("why is it") == '"why" OR "is" OR "it"'
    assert fts_query("???") is None


def test_identifier_expansion_matches_across_styles(tmp_path):
    index = HybridIndex(tmp_path / "i.db")
    index.add(
        [
            mk("code:a.py#L1-L1", "MAX_BACKOFF = 30"),
            mk("code:b.py#L1-L1", "policy = retry_policy()"),
            mk("code:c.py#L1-L1", "unrelated = 1"),
        ]
    )
    assert [h.chunk.id for h in index.search("max_backoff")] == ["code:a.py#L1-L1"]
    assert [h.chunk.id for h in index.search("RetryPolicy")] == ["code:b.py#L1-L1"]
    assert [h.chunk.id for h in index.search("backoff")] == ["code:a.py#L1-L1"]


# -- store: ranking --------------------------------------------------------------------------


class TableEmbedder:
    """Embedder whose vectors are looked up by the first word of the text (test control)."""

    def __init__(self, table: dict[str, list[float]], dim: int):
        self.table = table
        self.dim = dim
        self.name = "table"

    def _vec(self, text: str) -> np.ndarray:
        v = np.asarray(self.table.get(text.split()[0], [0.0] * self.dim), dtype=np.float32)
        return v / (np.linalg.norm(v) or 1.0)

    def embed(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts]) if texts else np.zeros((0, self.dim))

    def embed_query(self, text: str) -> np.ndarray:
        return self._vec(text)


def test_rrf_fuses_bm25_and_dense_rankings(tmp_path):
    # BM25 order for "alpha": A (dense tf) > B > C. Dense order: B > C > A.
    emb = TableEmbedder(
        {"alpha": [1, 0, 0], "A": [0.2, 1, 0], "B": [1, 0.05, 0], "C": [1, 0.3, 0]}, dim=3
    )
    assert isinstance(emb, Embedder)
    index = HybridIndex(tmp_path / "i.db", emb)
    index.add(
        [
            mk("code:a#L1-L1", "A alpha alpha alpha", title="A"),
            mk("code:b#L1-L1", "B alpha alpha beta gamma", title="B"),
            mk("code:c#L1-L1", "C alpha beta gamma delta epsilon zeta eta", title="C"),
        ]
    )
    hits = index.search("alpha", k=3)
    ranks = {h.chunk.id[5]: (h.bm25_rank, h.dense_rank) for h in hits}
    assert ranks == {"a": (1, 3), "b": (2, 1), "c": (3, 2)}
    assert [h.chunk.id[5] for h in hits] == ["b", "a", "c"]
    for h in hits:
        assert h.score == pytest.approx(1 / (RRF_K + h.bm25_rank) + 1 / (RRF_K + h.dense_rank))


def test_rrf_with_hash_embedder_scores_and_order(tmp_path):
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    index.add(
        [
            mk("commit:1111111", "Cap backoff at 30s to avoid huge sleeps", kind="commit"),
            mk("commit:2222222", "Add jitter to backoff, thundering herd", kind="commit"),
            mk("doc:README.md#retries", "Delays are capped at 30 seconds", kind="doc"),
            mk("code:x.py#L1-L3", "def parse(): pass", kind="code"),
        ]
    )
    hits = index.search("backoff capped sleeps", k=4)
    assert hits[0].chunk.id == "commit:1111111"
    assert hits[0].bm25_rank == 1 and hits[0].dense_rank == 1
    assert "code:x.py#L1-L3" not in {h.chunk.id for h in hits}  # no shared words, cosine 0
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    for h in hits:
        expected = sum(1 / (RRF_K + r) for r in (h.bm25_rank, h.dense_rank) if r is not None)
        assert h.score == pytest.approx(expected)


def test_bm25_only_without_embedder(tmp_path):
    index = HybridIndex(tmp_path / "i.db")
    index.add([mk("code:a#L1-L1", "backoff backoff"), mk("code:b#L1-L1", "backoff and more")])
    hits = index.search("backoff")
    assert [h.chunk.id for h in hits] == ["code:a#L1-L1", "code:b#L1-L1"]
    assert all(h.dense_rank is None for h in hits)
    assert [h.bm25_rank for h in hits] == [1, 2]
    assert index.search("backoff", k=0) == []


def test_kinds_filter(tmp_path):
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    index.add(
        [
            mk("code:a#L1-L1", "backoff code", kind="code"),
            mk("doc:r.md#x", "backoff docs", kind="doc"),
            mk("commit:abcdef1", "backoff commit", kind="commit"),
        ]
    )
    assert {h.chunk.kind for h in index.search("backoff", kinds=["doc"])} == {"doc"}
    assert {h.chunk.kind for h in index.search("backoff", kinds="commit")} == {"commit"}
    assert {h.chunk.kind for h in index.search("backoff", kinds=("code", "doc"))} == {
        "code",
        "doc",
    }
    assert index.search("backoff", kinds=[]) == []
    with pytest.raises(ValueError):
        index.search("backoff", kinds=["docs"])


# -- store: writes, persistence, embedder identity, threads ----------------------------------


def test_upsert_by_id_replaces_text_and_vector(tmp_path):
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    index.add([mk("code:a#L1-L1", "alpha")])
    index.add(
        [mk("code:a#L1-L1", "beta"), mk("code:b#L1-L1", "gamma"), mk("code:b#L1-L1", "delta")]
    )
    assert index.count() == 2
    assert index.get("code:a#L1-L1").text == "beta"
    assert index.get("code:b#L1-L1").text == "delta"  # last duplicate in a batch wins
    assert index.search("alpha") == []
    hit = index.search("beta")[0]
    assert hit.chunk.id == "code:a#L1-L1" and hit.dense_rank == 1


def test_get_count_ids_delete_clear(tmp_path):
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    index.add(
        [
            mk("code:a.py#L1-L2", "one", path="a.py"),
            mk("code:a.py#L3-L4", "two", path="a.py"),
            mk("doc:b.md#x", "three", kind="doc", path="b.md"),
            mk("commit:abcdef1", "four", kind="commit"),
        ]
    )
    assert index.get("missing") is None
    assert (index.count(), index.count("code"), index.count("commit")) == (4, 2, 1)
    assert index.ids("doc") == {"doc:b.md#x"}
    assert index.ids_by_path() == {
        "a.py": {"code:a.py#L1-L2", "code:a.py#L3-L4"},
        "b.md": {"doc:b.md#x"},
    }
    assert index.delete(["code:a.py#L1-L2", "nope"]) == 1
    assert index.search("one") == []
    index.set_meta("head_sha", "abc")
    index.clear()
    assert index.count() == 0 and index.get_meta("head_sha") is None
    with pytest.raises(ValueError):
        index.set_meta("embedder", "x")


def test_persistence_after_reopen(tmp_path):
    db = tmp_path / "nested" / "index.db"
    with HybridIndex(db, HashEmbedder()) as index:
        index.add([mk("code:a#L1-L1", "persistent backoff", symbol="backoff", start_line=1)])
        index.set_meta("head_sha", "deadbeef")
    with HybridIndex(db, HashEmbedder()) as index:
        assert index.get_meta("head_sha") == "deadbeef"
        chunk = index.get("code:a#L1-L1")
        assert chunk.symbol == "backoff" and chunk.start_line == 1
        hit = index.search("persistent")[0]
        assert hit.bm25_rank == 1 and hit.dense_rank == 1


def test_embedder_change_is_refused_or_rebuilt(tmp_path):
    db = tmp_path / "i.db"
    with HybridIndex(db, HashEmbedder(dim=256)) as index:
        index.add([mk("code:a#L1-L1", "backoff")])
    with pytest.raises(EmbedderMismatchError, match="on_embedder_change='rebuild'"):
        HybridIndex(db, HashEmbedder(dim=64))
    with HybridIndex(db, HashEmbedder(dim=64), on_embedder_change="rebuild") as index:
        assert index.get_meta("embedder_dim") == "64"
        assert index.search("backoff")[0].dense_rank == 1
    with HybridIndex(db) as index:  # no embedder: BM25-only, stored vectors untouched
        assert index.search("backoff")[0].dense_rank is None


def test_vectors_backfilled_when_embedder_added_later(tmp_path):
    db = tmp_path / "i.db"
    with HybridIndex(db) as index:
        index.add([mk("code:a#L1-L1", "backoff"), mk("code:b#L1-L1", "jitter")])
    with HybridIndex(db, HashEmbedder()) as index:
        assert index.search("jitter")[0].dense_rank == 1


def test_concurrent_searches_and_writes_from_threads(tmp_path):
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    index.add([mk(f"code:f{i}.py#L1-L1", f"backoff retry number{i}") for i in range(50)])
    barrier = threading.Barrier(8)

    def worker(n: int) -> int:
        barrier.wait()
        found = 0
        for j in range(25):
            if n % 4 == 0:
                index.add([mk(f"code:w{n}_{j}.py#L1-L1", f"backoff writer{n}")])
            found += len(index.search(f"backoff number{j}", k=5, kinds=["code"]))
            assert index.get(f"code:f{j}.py#L1-L1") is not None
        return found

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(8)))
    assert all(r == 25 * 5 for r in results)
    assert index.count() == 50 + 2 * 25


# -- embedders -------------------------------------------------------------------------------


class FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding; records batch sizes."""

    instances: ClassVar[list[FakeTextEmbedding]] = []

    def __init__(self, model_name: str, cache_dir=None, threads=None):
        self.model_name = model_name
        self.batches: list[int] = []
        FakeTextEmbedding.instances.append(self)

    @classmethod
    def get_embedding_size(cls, model_name: str) -> int:
        return 4

    def _gen(self, texts, batch_size):
        texts = list(texts)
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            self.batches.append(len(batch))
            for t in batch:
                yield np.array([len(t), 1.0, 0.0, 2.0], dtype=np.float64)

    def passage_embed(self, texts, batch_size=256, **kw):
        yield from self._gen(texts, batch_size)

    def query_embed(self, query, **kw):
        yield from self._gen([query], 256)


@pytest.fixture
def fake_fastembed(monkeypatch):
    FakeTextEmbedding.instances = []
    module = types.ModuleType("fastembed")
    module.TextEmbedding = FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", module)
    return module


def test_fastembed_embedder_lazy_batched_normalised(fake_fastembed):
    emb = FastEmbedEmbedder("some/model", batch_size=2)
    assert emb.dim == 4 and emb.name == "fastembed:some/model"
    assert FakeTextEmbedding.instances == []  # nothing loaded until first use
    out = emb.embed(["a", "bb", "ccc"])
    assert out.shape == (3, 4) and out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0)
    assert FakeTextEmbedding.instances[0].batches == [2, 1]
    q = emb.embed_query("hello")
    assert q.shape == (4,) and np.isclose(np.linalg.norm(q), 1.0)
    assert emb.embed([]).shape == (0, 4)
    emb.embed(["again"])
    assert len(FakeTextEmbedding.instances) == 1  # model loaded once
    assert isinstance(emb, Embedder)


def test_get_default_embedder(fake_fastembed, monkeypatch):
    monkeypatch.delenv("ARCH_EMBEDDER", raising=False)
    monkeypatch.delenv("ARCH_EMBED_MODEL", raising=False)
    emb = get_default_embedder()
    assert isinstance(emb, FastEmbedEmbedder) and emb.model == embed_mod.DEFAULT_EMBED_MODEL
    monkeypatch.setenv("ARCH_EMBED_MODEL", "other/model")
    assert get_default_embedder().model == "other/model"
    monkeypatch.setenv("ARCH_EMBEDDER", "none")
    assert get_default_embedder() is None


def test_get_default_embedder_without_fastembed(monkeypatch):
    monkeypatch.delenv("ARCH_EMBEDDER", raising=False)
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import fastembed` fail
    assert get_default_embedder() is None
    with pytest.raises(ImportError, match="embed"):
        _ = FastEmbedEmbedder().dim


@pytest.mark.live
def test_fastembed_real_model_live(tmp_path):
    pytest.importorskip("fastembed")
    emb = FastEmbedEmbedder()
    vecs = emb.embed(["exponential backoff with a cap", "markdown table of contents"])
    q = emb.embed_query("why is the retry delay capped?")
    assert vecs.shape == (2, emb.dim)
    assert float(vecs[0] @ q) > float(vecs[1] @ q)
    index = HybridIndex(tmp_path / "live.db", emb)
    index.add([mk("code:a#L1-L1", "exponential backoff with a cap")])
    assert index.search("retry delay")[0].dense_rank == 1


# -- build -----------------------------------------------------------------------------------


class StubRepo:
    """Duck-typed stand-in for sources.git.GitRepo."""

    def __init__(self, files: dict[str, str], commits: list[FakeCommit], head: str = "h1"):
        self.files = dict(files)
        self.commits = list(commits)
        self.head = head
        self.reads: list[str] = []
        self.unreadable: set[str] = set()

    def head_sha(self) -> str:
        return self.head

    def ls_files(self) -> list[str]:
        return sorted(self.files)

    def read_file(self, path: str, rev: str = "HEAD") -> str:
        self.reads.append(path)
        if path in self.unreadable:
            raise RuntimeError(f"cannot read {path}")
        return self.files[path]

    def iter_commits(self, max_count=None, paths=None):
        yield from self.commits[:max_count]


def stub_repo() -> StubRepo:
    files = {
        RETRY_PATH: RETRY_V3,
        "README.md": README_V2,
        "web/app.js": "function go() {\n  return 1;\n}\n",
        "node_modules/lib/index.js": "module.exports = 1;\n",
        "dist/bundle.js": "var a=1;\n",
        "static/app.min.js": "var a=1;\n",
        "uv.lock": "version = 1\n",
        "logo.png": "\x89PNG",
        "big.py": "x = 1\n" * 50_000,
        "blob.py": "abc\x00def\n",
        "gen_pb.py": "# Code generated by protoc. DO NOT EDIT.\nX = 1\n",
        "broken.py": "x = 1\n",
    }
    commits = [
        FakeCommit("c" * 40, "Add jitter to backoff (#15)", "Thundering herd.", [RETRY_PATH]),
        FakeCommit("b" * 40, "Cap backoff at 30s (#12)", "Fixes #7.", [RETRY_PATH]),
        FakeCommit("a" * 40, "Add retry helper", "", [RETRY_PATH, "README.md"]),
    ]
    repo = StubRepo(files, commits)
    repo.unreadable.add("broken.py")
    return repo


def test_filters():
    assert is_included("src/a.py") and is_included("Makefile") and is_included("docs/X.MD")
    assert not is_included("logo.png") and not is_included("data.csv")
    assert is_excluded("node_modules/x/index.js") and is_excluded("pkg/node_modules/a.js")
    assert is_excluded("dist/bundle.js") and not is_excluded("src/build/core.py")
    assert is_excluded("app.min.js") and is_excluded("poetry.lock") and is_excluded("go.sum")
    assert not is_excluded("src/pkg/retry.py")


def test_build_index_with_stub_repo(tmp_path):
    repo = stub_repo()
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    events: list[tuple[str, int, int | None]] = []
    stats = build_index(
        repo, index, max_file_bytes=100_000, workers=4, progress=lambda *e: events.append(e)
    )
    assert events[0] == ("files", 7, 7) and events[-1] == ("commits", 3, 3)
    assert stats["skipped"] is False and stats["head_sha"] == "h1"
    assert stats["files"] == {
        "indexed": 3,  # retry.py, README.md, web/app.js
        "unchanged": 0,
        "excluded": 5,  # node_modules, dist, .min.js, uv.lock, .png
        "binary": 1,
        "too_large": 1,
        "generated": 1,
        "unreadable": 1,
    }
    assert (stats["code"], stats["doc"], stats["commit"]) == (6, 3, 3)
    assert stats["added"] == {"code": 6, "doc": 3, "commit": 3}
    assert stats["commits"] == 3
    assert index.count() == 12
    assert {p for p in index.ids_by_path()} == {RETRY_PATH, "README.md", "web/app.js"}
    assert index.get(code_ref(RETRY_PATH, 20, 26)).symbol == "Retrier.run"
    assert index.get(doc_ref("README.md", "settings")) is not None
    top = index.search("thundering herd jitter", kinds=["commit"])[0]
    assert top.chunk.id == commit_ref("c" * 40)
    assert index.get_meta("head_sha") == "h1"
    assert "node_modules/lib/index.js" not in repo.reads and "logo.png" not in repo.reads


def test_build_index_incremental(tmp_path):
    repo = stub_repo()
    index = HybridIndex(tmp_path / "i.db", HashEmbedder())
    build_index(repo, index)
    reads = len(repo.reads)

    again = build_index(repo, index)
    assert again["skipped"] is True and again["head_sha"] == "h1"
    assert again["code"] == index.count("code")
    assert len(repo.reads) == reads  # nothing read

    # new HEAD: one file changed, one deleted, one new commit
    repo.head = "h2"
    repo.files[RETRY_PATH] = RETRY_V3 + "\n\ndef extra():\n    return 3\n"
    del repo.files["web/app.js"]
    repo.commits.insert(0, FakeCommit("d" * 40, "Add extra helper", "", [RETRY_PATH]))
    stats = build_index(repo, index)
    assert stats["skipped"] is False
    assert stats["files"]["indexed"] == 1 and stats["files"]["unchanged"] == 1
    assert stats["added"] == {"code": 6, "doc": 0, "commit": 1}
    assert stats["removed"] == 1  # web/app.js
    assert index.get(code_ref("web/app.js", 1, 3)) is None
    assert index.get(code_ref(RETRY_PATH, 29, 30)).symbol == "extra"
    assert stats["commit"] == 4 and index.get_meta("head_sha") == "h2"

    # rewritten history drops unreachable commits
    repo.head = "h3"
    repo.commits = repo.commits[:2]
    stats = build_index(repo, index)
    assert stats["commit"] == 2 and stats["removed"] == 2


def test_build_index_force_and_settings_change_rebuild(tmp_path):
    repo = stub_repo()
    index = HybridIndex(tmp_path / "i.db")
    build_index(repo, index)
    assert build_index(repo, index, force=True)["files"]["indexed"] == 3
    changed = build_index(repo, index, max_commits=1)
    assert changed["skipped"] is False and changed["commit"] == 1
    assert build_index(repo, index, max_commits=1)["skipped"] is True
    no_commits = build_index(repo, index, max_commits=0)
    assert no_commits["commit"] == 0 and no_commits["code"] > 0


def test_build_index_empty_repo(tmp_path):
    repo = StubRepo({}, [], head="")
    stats = build_index(repo, HybridIndex(tmp_path / "i.db"))
    assert stats["skipped"] is False and stats["head_sha"] is None
    assert (stats["code"], stats["doc"], stats["commit"]) == (0, 0, 0)


def test_build_index_on_toy_repo_with_real_git(toy_repo_shas, tmp_path):
    git = pytest.importorskip("archaeologist.sources.git")
    path, shas = toy_repo_shas
    repo = git.GitRepo(path)
    index = HybridIndex(Path(tmp_path) / "toy.db", HashEmbedder())
    stats = build_index(repo, index)
    assert stats["skipped"] is False and stats["head_sha"] == shas[-1]
    assert stats["commit"] == 4 and stats["doc"] == 3 and stats["code"] == 5
    assert index.get(code_ref(RETRY_PATH, 20, 26)).symbol == "Retrier.run"
    commit_hits = index.search("thundering herd", kinds=["commit"])
    assert commit_hits[0].chunk.id == commit_ref(shas[2])
    assert commit_hits[0].chunk.meta["refs"] == ["pr:15", "gh:9"]
    doc_hits = index.search("MAX_BACKOFF cap", kinds=["doc"])
    assert doc_hits[0].chunk.id == doc_ref("README.md", "settings")
    assert build_index(repo, index)["skipped"] is True


# -- review regressions ----------------------------------------------------------------------


class InvalidProtobuf(Exception):
    """Like onnxruntime's errors: subclasses Exception, not OSError/RuntimeError."""


class FailingQueryEmbedder(HashEmbedder):
    def __init__(self, exc: Exception):
        super().__init__()
        self.exc = exc

    def embed_query(self, text: str) -> np.ndarray:
        raise self.exc


@pytest.mark.parametrize("exc", [OSError("model download failed"), InvalidProtobuf("truncated")])
def test_search_falls_back_to_bm25_when_the_query_embedding_fails(tmp_path, exc, caplog):
    index = HybridIndex(tmp_path / "i.db", FailingQueryEmbedder(exc))
    index.add([mk("code:a#L1-L1", "backoff"), mk("code:b#L1-L1", "jitter")])
    with caplog.at_level("WARNING", logger="archaeologist.index.store"):
        hits = index.search("backoff")
        assert [h.chunk.id for h in index.search("jitter")] == ["code:b#L1-L1"]
    assert [h.chunk.id for h in hits] == ["code:a#L1-L1"] and hits[0].dense_rank is None
    assert caplog.text.count("query embedding failed") == 1  # warned once, not per search


class BatchEmbedder(HashEmbedder):
    """Records batch sizes; fails (like an offline model download) after `ok_batches`."""

    def __init__(self, ok_batches: int):
        super().__init__()
        self.ok_batches = ok_batches
        self.batches: list[int] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        if len(self.batches) >= self.ok_batches:
            raise OSError("offline: cannot download model")
        self.batches.append(len(texts))
        return super().embed(texts)


def test_backfill_is_paged_and_survives_a_failing_embedder(tmp_path, caplog):
    db = tmp_path / "i.db"
    with HybridIndex(db) as index:  # built BM25-only
        index.add([mk(f"code:f{i}.py#L1-L1", f"backoff number{i}") for i in range(600)])
    def hit_for(index: HybridIndex, cid: str):
        return next(h for h in index.search("number599", k=10) if h.chunk.id == cid)

    flaky = BatchEmbedder(ok_batches=1)
    with (
        caplog.at_level("WARNING", logger="archaeologist.index.store"),
        HybridIndex(db, flaky) as index,  # opens instead of raising
    ):
        assert flaky.batches == [256]
        hit = hit_for(index, "code:f599.py#L1-L1")  # not embedded yet: found by BM25
        assert hit.bm25_rank == 1 and hit.dense_rank is None
    assert "backfill stopped after 256 of 600 chunks" in caplog.text
    resumed = BatchEmbedder(ok_batches=10)
    with HybridIndex(db, resumed) as index:  # a later open embeds only what is missing
        assert resumed.batches == [256, 88]
        assert hit_for(index, "code:f599.py#L1-L1").dense_rank is not None


def test_build_index_skips_oversized_blobs_without_reading_them(tmp_path):
    repo = stub_repo()
    repo.file_sizes = lambda: {p: len(t.encode()) for p, t in repo.files.items()}
    stats = build_index(repo, HybridIndex(tmp_path / "i.db"), max_file_bytes=100_000)
    assert stats["files"]["too_large"] == 1 and stats["files"]["indexed"] == 3
    assert "big.py" not in repo.reads


def test_fastembed_load_failure_is_remembered_for_a_while(fake_fastembed, monkeypatch):
    attempts: list[str] = []

    class Offline(FakeTextEmbedding):
        def __init__(self, model_name, cache_dir=None, threads=None):
            attempts.append(model_name)
            raise OSError("no network and no cached model")

    fake_fastembed.TextEmbedding = Offline
    clock = [1000.0]
    monkeypatch.setattr(embed_mod.time, "monotonic", lambda: clock[0])
    emb = FastEmbedEmbedder("m")
    with pytest.raises(OSError):
        emb.embed_query("x")
    with pytest.raises(RuntimeError, match="failed to load"):
        emb.embed_query("x")
    assert attempts == ["m"]  # the second call did not try the download again
    clock[0] += embed_mod.LOAD_RETRY_S + 1
    with pytest.raises(OSError):
        emb.embed_query("x")
    assert attempts == ["m", "m"]
