"""RepoToolbox over the real toy repo: GitRepo + HybridIndex (HashEmbedder) + a real
GitHubClient talking to a fake GitHub (httpx.MockTransport). No network."""

from __future__ import annotations

import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from toy_github import P, ToyGitHub

from archaeologist.config import repo_data_dir
from archaeologist.contracts import Toolbox
from archaeologist.index.build import build_index
from archaeologist.index.store import HybridIndex
from archaeologist.models import Evidence
from archaeologist.sources.git import GitRepo
from archaeologist.testing import HashEmbedder
from archaeologist.toolbox import (
    GITHUB_CACHE_FILE,
    INDEX_FILE,
    MAX_COMMITS_META,
    RepoToolbox,
    open_toolbox,
)

BACKOFF_ID = "code:src/pkg/retry.py#L8-L13"


@pytest.fixture(autouse=True)
def _no_ambient_github(monkeypatch):
    """Keep real GitHub credentials (env or `gh`) out of these tests."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    real_run = subprocess.run

    def run(args, *a, **kw):
        if args and args[0] == "gh":
            raise FileNotFoundError("gh")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """GitHubClient sleeps before retrying 5xx answers; tests need not wait."""
    import archaeologist.sources.github as gh

    monkeypatch.setattr(gh.time, "sleep", lambda s: None)


@pytest.fixture
def shas(toy_repo_shas) -> list[str]:
    return toy_repo_shas[1]


@pytest.fixture
def repo(toy_repo_shas) -> GitRepo:
    return GitRepo(toy_repo_shas[0])


@pytest.fixture
def index(repo, tmp_path) -> HybridIndex:
    idx = HybridIndex(tmp_path / "index.sqlite", HashEmbedder())
    build_index(repo, idx)
    yield idx
    idx.close()


@pytest.fixture
def fake(shas) -> ToyGitHub:
    return ToyGitHub(shas)


@pytest.fixture
def tb(repo, index, fake) -> RepoToolbox:
    return RepoToolbox(repo, index, fake.client())


@pytest.fixture
def offline(repo, index) -> RepoToolbox:
    return RepoToolbox(repo, index)


def ids(items: list[Evidence]) -> list[str]:
    return [e.id for e in items]


def commit(sha: str) -> str:
    return f"commit:{sha}"


# -- construction ----------------------------------------------------------------------------


def test_is_a_toolbox_and_knows_its_repo(tb, offline):
    assert isinstance(tb, Toolbox)
    assert (tb.owner, tb.name) == ("acme", "toy")
    assert (offline.owner, offline.name) == ("acme", "toy")  # from the remote, even offline


# -- search ----------------------------------------------------------------------------------


def test_search_code_returns_located_code_evidence(tb):
    hits = tb.search("code", "backoff cap", k=3)
    assert hits and all(e.kind == "code" and e.source == "search:code" for e in hits)
    backoff = next(e for e in hits if e.id == BACKOFF_ID)
    assert backoff.meta["path"] == "src/pkg/retry.py"
    assert (backoff.meta["start_line"], backoff.meta["end_line"]) == (8, 13)
    assert backoff.meta["symbol"] == "backoff"
    assert backoff.score > 0
    assert backoff.url and backoff.url.startswith("https://github.com/acme/toy/blob/")
    assert backoff.url.endswith("/src/pkg/retry.py#L8-L13")


def test_search_docs_and_commits(tb, shas):
    docs = tb.search("docs", "retry settings", k=3)
    assert docs and all(e.kind == "doc" for e in docs)
    assert "doc:README.md#settings" in ids(docs)

    commits = tb.search("commits", "cap backoff huge sleeps", k=4)
    assert commits and all(e.kind == "commit" and e.source == "search:commits" for e in commits)
    c2 = next(e for e in commits if e.id == commit(shas[1]))
    assert c2.refs[:2] == ["pr:12", "issue:7"]
    assert c2.url == f"https://github.com/acme/toy/commit/{shas[1]}"
    assert "refs" not in c2.meta  # Evidence.refs is the single source of links


def test_search_prs_uses_github(tb):
    found = tb.search("prs", "thundering herd", k=5)
    assert set(ids(found)) == {"pr:15", "issue:9"}
    assert all(e.source == "search:prs" for e in found)
    assert next(e for e in found if e.id == "pr:15").kind == "pr"


def test_search_prs_offline_or_failing_is_empty(offline, tb, fake, caplog):
    assert offline.search("prs", "backoff") == []
    fake.fail["/search/issues"] = 403
    with caplog.at_level(logging.WARNING, logger="archaeologist.toolbox"):
        assert tb.search("prs", "backoff") == []
    assert "GitHub search failed" in caplog.text


def test_search_validates_source_and_handles_empty_queries(tb):
    with pytest.raises(ValueError):
        tb.search("wiki", "anything")
    assert tb.search("code", "   ") == []
    assert tb.search("code", "backoff", k=0) == []


# -- history ---------------------------------------------------------------------------------


def test_history_by_symbol_newest_first_with_pr_refs(tb, shas):
    hist = tb.history("src/pkg/retry.py", symbol="backoff")
    assert ids(hist) == [commit(shas[2]), commit(shas[1]), commit(shas[0])]
    assert all(e.kind == "commit" and e.source == "history" for e in hist)
    by_id = {e.id: e for e in hist}
    assert by_id[commit(shas[1])].refs[:2] == ["pr:12", "issue:7"]
    assert by_id[commit(shas[2])].refs[0] == "pr:15"
    # c1's message names no PR: GitHub's commit -> PR association fills it in
    assert by_id[commit(shas[0])].refs == ["pr:3"]
    assert "Diff:" in by_id[commit(shas[1])].text


def test_history_by_lines_and_by_path(tb, shas):
    by_lines = tb.history("src/pkg/retry.py", 16, 26)  # class Retrier, added in c3
    assert ids(by_lines)[0] == commit(shas[2])
    by_path = tb.history("README.md")
    assert ids(by_path) == [commit(shas[3]), commit(shas[0])]
    assert tb.history("README.md", k=1) == by_path[:1]


def test_history_symbol_falls_back_to_line_range(tb, shas):
    # unknown symbol in a missing file: nothing; a line range of a real file still works
    assert tb.history("nope.py", symbol="ghost") == []
    hist = tb.history("src/pkg/retry.py", 8, 13, symbol="backoff", k=2)
    assert ids(hist) == [commit(shas[2]), commit(shas[1])]


def test_history_asks_github_only_for_commits_without_a_pr_and_caches(tb, fake, shas):
    tb.history("src/pkg/retry.py", symbol="backoff")
    tb.history("src/pkg/retry.py", symbol="backoff")
    assert fake.hits(f"{P}/commits/{shas[0]}/pulls") == 1
    assert fake.hits(f"{P}/commits/{shas[1]}/pulls") == 0  # "(#12)" already names the PR
    assert fake.hits(f"{P}/commits/{shas[2]}/pulls") == 0


def test_history_ignores_github_errors_and_works_offline(tb, offline, fake, shas):
    fake.fail[f"{P}/commits/{shas[0]}/pulls"] = 403
    hist = tb.history("src/pkg/retry.py", symbol="backoff")
    assert ids(hist)[-1] == commit(shas[0])
    assert hist[-1].refs == []
    assert ids(offline.history("src/pkg/retry.py", symbol="backoff")) == ids(hist)


def test_failed_pr_lookups_are_not_repeated_within_a_call(tb, fake, shas):
    # README's commits (c4, c1) both lack a PR ref: looked up concurrently
    for sha in (shas[0], shas[3]):
        fake.fail[f"{P}/commits/{sha}/pulls"] = 403
    hist = tb.history("README.md")
    assert [e.refs for e in hist] == [[], []]
    assert fake.hits(f"{P}/commits/{shas[0]}/pulls") == 1
    assert fake.hits(f"{P}/commits/{shas[3]}/pulls") == 1
    del fake.fail[f"{P}/commits/{shas[0]}/pulls"]
    assert tb.history("README.md")[1].refs == ["pr:3"]  # failures are not cached


# -- fetch -----------------------------------------------------------------------------------


def test_fetch_commit_by_short_sha(tb, shas):
    item = tb.fetch(f"commit:{shas[1][:7]}")
    assert item is not None
    assert item.id == commit(shas[1]) and item.kind == "commit" and item.source == "fetch"
    assert item.refs[:2] == ["pr:12", "issue:7"]
    assert "min(2 ** attempt, MAX_BACKOFF)" in item.text  # the diff


def test_fetch_commit_gets_pr_from_github(tb, offline, shas):
    assert tb.fetch(commit(shas[0])).refs == ["pr:3"]
    assert offline.fetch(commit(shas[0])).refs == []


def test_fetch_pr_with_discussion_and_links(tb, shas):
    pr = tb.fetch("pr:12")
    assert pr is not None and pr.id == "pr:12" and pr.kind == "pr"
    assert "issue:7" in pr.refs and commit(shas[1]) in pr.refs
    assert "gateway timeout" in pr.text  # conversation comment
    assert "configurable" in pr.text  # review comment
    assert "Thanks for contributing" not in pr.text  # template comment stripped


def test_fetch_issue_and_gh_refs(tb):
    issue = tb.fetch("issue:7")
    assert issue is not None and issue.id == "issue:7" and issue.kind == "issue"
    assert "a cap around 30 seconds" in issue.text
    # an "issue" that is really a PR comes back as the PR
    as_pr = tb.fetch("issue:12")
    assert as_pr is not None and as_pr.id == "pr:12" and "gateway timeout" in as_pr.text
    assert tb.fetch("gh:7").id == "issue:7"
    assert tb.fetch("gh:12").id == "pr:12"


def test_fetch_pr_number_that_is_an_issue(tb):
    item = tb.fetch("pr:7")
    assert item is not None and item.id == "issue:7"


def test_fetch_code_reads_lines_at_head(tb):
    item = tb.fetch(BACKOFF_ID)
    assert item is not None and item.id == BACKOFF_ID and item.kind == "code"
    assert item.meta == {
        "path": "src/pkg/retry.py",
        "start_line": 8,
        "end_line": 13,
        "symbol": "backoff",
    }
    assert item.text.splitlines()[1] == "def backoff(attempt, jitter=True):"
    clipped = tb.fetch("code:src/pkg/retry.py#L25-L999")
    assert clipped is not None and clipped.id.endswith("#L25-L26")
    whole = tb.fetch("code:README.md")
    assert whole is not None and whole.meta["start_line"] == 1


def test_fetch_doc_from_index(tb):
    item = tb.fetch("doc:README.md#retries")
    assert item is not None and item.kind == "doc"
    assert "capped at 30 seconds" in item.text


@pytest.mark.parametrize(
    "ref",
    [
        "nonsense",
        "pr:abc",
        "pr:999",
        "issue:404",
        "gh:404",
        "commit:zzzz",
        "commit:deadbeefdeadbeef",
        "code:missing.py#L1-L2",
        "code:src/pkg/retry.py#L500-L510",
        "code:../outside.py#L1-L2",
        "doc:README.md#nope",
        "wiki:Home",
        "",
    ],
)
def test_fetch_malformed_or_unknown_is_none(tb, ref):
    assert tb.fetch(ref) is None


def test_fetch_github_refs_offline_or_failing_is_none(offline, tb, fake):
    assert offline.fetch("pr:12") is None
    assert offline.fetch("issue:7") is None
    assert offline.fetch("gh:7") is None
    fake.fail[f"{P}/pulls/12"] = 500
    fake.fail[f"{P}/issues/12"] = 403
    assert tb.fetch("pr:12") is None


def test_rate_limit_pauses_github(tb, fake, caplog):
    fake.fail[f"{P}/pulls/12"] = (429, {"Retry-After": "120"})
    tb.github.max_wait_s = 0  # too long to wait: the client raises RateLimited
    with caplog.at_level(logging.WARNING, logger="archaeologist.toolbox"):
        assert tb.fetch("pr:12") is None
    assert "skipping GitHub calls" in caplog.text
    n = len(fake.requests)
    assert tb.fetch("issue:7") is None  # skipped while paused: no request at all
    assert len(fake.requests) == n


# -- concurrency -----------------------------------------------------------------------------


def test_concurrent_calls_match_sequential(tb, shas):
    calls = [
        ("search", ("code", "backoff jitter")),
        ("search", ("commits", "cap backoff")),
        ("search", ("prs", "thundering herd")),
        ("history", ("src/pkg/retry.py", None, None, "backoff")),
        ("history", ("README.md",)),
        ("fetch", ("pr:12",)),
        ("fetch", ("issue:7",)),
        ("fetch", (commit(shas[0]),)),
        ("fetch", (BACKOFF_ID,)),
    ] * 4

    def run(call):
        name, args = call
        out = getattr(tb, name)(*args)
        return [e.model_dump() for e in out] if isinstance(out, list) else out.model_dump()

    sequential = [run(c) for c in calls]
    with ThreadPoolExecutor(max_workers=8) as pool:
        parallel = list(pool.map(run, calls))
    assert parallel == sequential


# -- open_toolbox ----------------------------------------------------------------------------


def test_open_toolbox_offline_builds_index(toy_repo):
    tb = open_toolbox(toy_repo, offline=True, embedder=HashEmbedder())
    with tb:
        assert tb.github is None
        assert (tb.owner, tb.name) == ("acme", "toy")
        assert tb.build_stats is not None and not tb.build_stats["skipped"]
        assert tb.index.count("code") > 0 and tb.index.count("commit") == 4
        assert Path(tb.index.db_path) == repo_data_dir(toy_repo) / INDEX_FILE
        assert tb.index.get_meta(MAX_COMMITS_META) == "5000"
        assert tb.search("code", "backoff")
    again = open_toolbox(toy_repo, offline=True, embedder=HashEmbedder())
    with again:
        assert again.build_stats["skipped"]  # incremental: HEAD unchanged


def test_open_toolbox_online_uses_cached_github_client(toy_repo, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    with open_toolbox(toy_repo, embedder=None, build=False) as tb:
        assert tb.github is not None
        assert (tb.github.owner, tb.github.repo) == ("acme", "toy")
        assert tb.github.authenticated
        assert tb.build_stats is None and tb.index.count() == 0
    assert (repo_data_dir(toy_repo) / GITHUB_CACHE_FILE).exists()


def test_open_toolbox_without_github_remote(toy_repo):
    subprocess.run(["git", "remote", "remove", "origin"], cwd=toy_repo, check=True)
    with open_toolbox(toy_repo, embedder=None) as tb:
        assert tb.github is None and tb.owner is None
        hits = tb.search("code", "backoff")
        assert hits and hits[0].url is None


def test_open_toolbox_rejects_non_repo(tmp_path):
    with pytest.raises(ValueError):
        open_toolbox(tmp_path, offline=True, embedder=None)


# -- review regressions ----------------------------------------------------------------------


def _small_repo(root: Path, commits: list[tuple[str, dict[str, str]]]) -> GitRepo:
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "D",
        "GIT_AUTHOR_EMAIL": "d@x",
        "GIT_COMMITTER_NAME": "D",
        "GIT_COMMITTER_EMAIL": "d@x",
        "PATH": __import__("os").environ["PATH"],
    }

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )

    root.mkdir(parents=True)
    git("init", "-q", "-b", "main")
    for message, files in commits:
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(text.encode())
            git("add", "--", rel)
        git("commit", "-q", "-m", message)
    return GitRepo(root)


def test_history_symbol_with_lines_traces_that_definition(tmp_path):
    """A setter chunk (symbol Box.size, lines 6-10) gets the setter's history, not the
    getter's, although both definitions are named Box.size."""
    from test_git import BOX_V1, BOX_V2, BOX_V3

    from archaeologist.index.chunkers import chunk_python

    repo = _small_repo(
        tmp_path / "box",
        [
            ("Add Box", {"box.py": BOX_V1}),
            ("Validate size in setter (#42)", {"box.py": BOX_V2}),
            ("Getter returns int (#43)", {"box.py": BOX_V3}),
        ],
    )
    setter = [c for c in chunk_python("box.py", BOX_V3) if c.symbol == "Box.size"][-1]
    tb = RepoToolbox(repo, HybridIndex(":memory:"), owner="o", name="r")
    got = tb.history("box.py", setter.start_line, setter.end_line, setter.symbol)
    assert [e.title for e in got] == ["Validate size in setter (#42)", "Add Box"]


def test_history_by_path_is_bounded_by_the_git_timeout(offline, monkeypatch):
    seen: dict = {}
    real = offline.repo.iter_commits

    def spy(*args, **kw):
        seen.update(kw)
        return real(*args, **kw)

    monkeypatch.setattr(offline.repo, "iter_commits", spy)
    assert offline.history("src/pkg/retry.py")
    assert seen["timeout"] == offline.repo.timeout


def test_fetch_code_counts_lines_like_the_chunkers_and_allows_hash_in_paths(tmp_path):
    from archaeologist.index.chunkers import chunk_python

    paged = "import os\n\x0c\n# section two\n\x0c\nx = 1\n\x0c\ndef target():\n    return 1\n"
    repo = _small_repo(
        tmp_path / "ff", [("add", {"m.py": paged, "F#/lib.fs": "let x = 1\nlet y = 2\n"})]
    )
    index = HybridIndex(":memory:")
    build_index(repo, index)
    (chunk,) = [c for c in chunk_python("m.py", paged) if c.symbol == "target"]
    assert chunk.id == "code:m.py#L7-L8"
    tb = RepoToolbox(repo, index, owner="o", name="r")
    item = tb.fetch(chunk.id)
    assert item is not None and item.text == "m.py:7-8 target\ndef target():\n    return 1"
    fs = tb.fetch("code:F#/lib.fs#L1-L2")
    assert fs is not None and fs.id == "code:F#/lib.fs#L1-L2" and "let y = 2" in fs.text
    whole = tb.fetch("code:F#/lib.fs")
    assert whole is not None and whole.meta["end_line"] == 2


def test_search_prs_hits_are_marked_partial(tb):
    hits = tb.search("prs", "backoff")
    assert hits and all(h.meta.get("partial") is True for h in hits)
    assert "partial" not in tb.fetch("pr:12").meta


def test_search_rate_limit_does_not_pause_core_fetches(tb, fake):
    import time

    reset = str(int(time.time()) + 50)
    fake.fail["/search/issues"] = (
        403,
        {"x-ratelimit-resource": "search", "x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
    )
    tb.github.max_wait_s = 0
    assert tb.search("prs", "backoff") == []
    assert tb.fetch("pr:12") is not None  # the core budget is untouched
    n = fake.hits("/search/issues")
    assert tb.search("prs", "jitter") == [] and fake.hits("/search/issues") == n  # still paused


def test_transferred_issue_is_not_read_as_this_repos(tb, fake):
    """GitHub answers 301 for an issue moved to another repository; following it must not
    turn acme/other#345 (and its refs) into acme/toy items."""
    fake.fail[f"{P}/issues/7"] = (
        301,
        {"location": "https://api.github.com/repositories/999/issues/345"},
    )
    fake.routes["/repositories/999/issues/345"] = {
        "number": 345,
        "title": "Moved issue",
        "body": "See #12 and #3.",
        "state": "open",
        "html_url": "https://github.com/acme/other/issues/345",
        "repository_url": "https://api.github.com/repos/acme/other",
        "user": {"login": "erin", "type": "User"},
        "comments": 0,
    }
    assert tb.fetch("issue:7") is None
    assert tb.fetch("gh:7") is None
    assert tb.fetch("issue:7") is None  # also when served from the cache


def test_index_errors_from_the_embedder_never_escape_search(repo, tmp_path):
    class InvalidProtobuf(Exception):
        """Like onnxruntime's errors: subclasses Exception, not OSError/RuntimeError."""

    class BrokenModel(HashEmbedder):
        def embed_query(self, text):
            raise InvalidProtobuf("truncated model file")

    index = HybridIndex(tmp_path / "i.sqlite", BrokenModel())
    build_index(repo, index)
    hits = RepoToolbox(repo, index).search("code", "backoff")
    assert hits and hits[0].id == BACKOFF_ID


def test_open_toolbox_warns_about_shallow_clones(toy_repo, tmp_path, caplog):
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{toy_repo}", str(clone)],
        check=True,
        capture_output=True,
    )
    with (
        caplog.at_level(logging.WARNING, logger="archaeologist.toolbox"),
        open_toolbox(clone, offline=True, embedder=None) as tb,
    ):
        (only,) = tb.history("src/pkg/retry.py", symbol="backoff")
    assert "shallow clone" in caplog.text
    assert only.meta["shallow_boundary"] is True
