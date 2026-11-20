"""RepoToolbox: the concrete `contracts.Toolbox` over a local clone, its hybrid index and
(optionally) GitHub.

- search  code / docs / commits -> the local `HybridIndex`; prs -> GitHub search
- history -> `git log -L` (symbol or line range) or the path's log, newest first
- fetch   -> canonical refs (commit:/pr:/issue:/gh:/code:/doc:) resolved to Evidence

Commit Evidence (from search, history or fetch) always names its PR when one is known:
a "(#n)" squash subject or "Merge pull request #n" subject gives pr:n directly; otherwise
GitHub's commit -> PR association is asked once per commit (cached).

GitHub is optional: without a client (offline, or no GitHub remote) PR search returns [] and
pr/issue/gh refs resolve to None. GitHub failures never raise out of the toolbox: they are
logged and the call degrades to []/None. After a rate limit, calls charged to that limit
(search, or the rest of the REST API) are skipped until it resets so the agent does not stall
on every call; the other budget keeps working.

Thread safety: GitRepo, HybridIndex and GitHubClient are each safe to share between threads,
and the toolbox's own caches are guarded by a lock, so one instance serves the graph's
concurrent fan-out.
"""

from __future__ import annotations

import logging
import posixpath
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self, TypeVar

import httpx

from .config import repo_data_dir
from .contracts import SEARCH_SOURCES, Embedder
from .index.build import ProgressFn, build_index
from .index.chunkers import split_lines
from .index.embed import get_default_embedder
from .index.store import HybridIndex
from .models import Chunk, Evidence, Hit
from .refs import code_ref, commit_ref, extract_refs, parse_ref, pr_ref, same_ref
from .sources.git import CommitInfo, GitRepo
from .sources.github import GitHubClient, GitHubError, Issue, PullRequest, RateLimited

log = logging.getLogger(__name__)

T = TypeVar("T")

INDEX_FILE = "index.sqlite"
GITHUB_CACHE_FILE = "github_cache.sqlite"
DEFAULT_MAX_COMMITS = 5000
MAX_COMMITS_META = "arch_max_commits"  # commit cap of the last build, reused by later builds

# Toolbox.search source -> HybridIndex chunk kind
_INDEX_KINDS = {"code": "code", "docs": "doc", "commits": "commit"}
_MAX_PRS_PER_COMMIT = 3  # PR refs added to a commit from GitHub's commit -> PR association
_GITHUB_WORKERS = 6  # concurrent commit -> PR lookups within one call
_MAX_FETCH_LINES = 400  # cap for code:<path>#L<a>-L<b> fetches
_DEFAULT_RATE_LIMIT_PAUSE_S = 60.0

_SHA = re.compile(r"[0-9a-fA-F]{4,64}\Z")
_NUMBER = re.compile(r"\d{1,9}\Z")
# The line suffix is matched from the right, so paths may contain '#' ("F#/lib.fs#L1-L2").
_CODE_VALUE = re.compile(r"(?P<path>.+?)(?:#L(?P<a>\d+)(?:-L?(?P<b>\d+))?)?\Z", re.DOTALL)
_MERGE_PR_SUBJECT = re.compile(r"^Merge pull request #(\d+)\b")
# Malformed payloads surface as these (pydantic's ValidationError is a ValueError).
_GITHUB_ERRORS = (GitHubError, httpx.HTTPError, sqlite3.Error, ValueError, KeyError, TypeError)
# Index search failures worth degrading on: SQLite trouble, or the embedding model failing
# to download / load (network errors are OSErrors, ONNX runtime errors RuntimeErrors).
_INDEX_ERRORS = (sqlite3.Error, OSError, RuntimeError, ValueError)

EmbedderChoice = Embedder | Literal["default"] | None


class RepoToolbox:
    """`contracts.Toolbox` over one repository.

    `owner`/`name` identify the GitHub repository; they default to the GitHub client's,
    then to the clone's GitHub remote. They are used for links and to keep refs that point
    at other repositories out of Evidence.refs, even when `github` is None (offline).
    """

    def __init__(
        self,
        repo: GitRepo,
        index: HybridIndex,
        github: GitHubClient | None = None,
        owner: str | None = None,
        name: str | None = None,
    ):
        self.repo = repo
        self.index = index
        self.github = github
        if (owner is None or name is None) and github is not None:
            owner, name = owner or github.owner, name or github.repo
        if owner is None or name is None:
            slug = repo.remote_slug()
            if slug:
                owner, name = owner or slug[0], name or slug[1]
        self.owner = owner
        self.name = name
        self.build_stats: dict[str, Any] | None = None  # set by open_toolbox when it builds
        self._lock = threading.Lock()
        self._pr_cache: dict[str, list[str]] = {}  # commit sha -> PR refs from GitHub
        self._gh_paused_until: dict[str, float] = {}  # rate-limit resource -> resume time
        self._head: str | None = None

    # -- contracts.Toolbox ------------------------------------------------------------------

    def search(self, source: str, query: str, k: int = 6) -> list[Evidence]:
        """Search one source (see contracts.Toolbox.search). Unknown source -> ValueError;
        index or GitHub failures are logged and give []."""
        if source not in SEARCH_SOURCES:
            raise ValueError(f"unknown search source {source!r}; expected one of {SEARCH_SOURCES}")
        if k <= 0 or not query.strip():
            return []
        if source == "prs":
            return self._search_github(query, k)
        try:
            hits = self.index.search(query, k=k, kinds=[_INDEX_KINDS[source]])
        except _INDEX_ERRORS as exc:
            log.warning("index search (%s) failed for %r: %s", source, query, exc)
            return []
        if source == "commits":
            return self._commit_hits(hits)
        return [self._chunk_evidence(h.chunk, f"search:{source}", h) for h in hits]

    def history(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        symbol: str | None = None,
        k: int = 6,
    ) -> list[Evidence]:
        """Commits that shaped this code, newest first.

        symbol given -> `git log -L` on that definition (the one overlapping the line range
        when the name is defined more than once, e.g. a property setter), falling back to the
        line range when it finds nothing; else a line range -> `git log -L` on the range; else
        the path's history (bounded by the repo's git timeout). Each commit is kind="commit"
        Evidence whose refs include its PR.
        """
        path = (path or "").strip()
        symbol = (symbol or "").strip() or None
        if k <= 0 or not path:
            return []
        commits: list[CommitInfo] = []
        end = None
        if start_line is not None:
            end = end_line if end_line is not None and end_line >= start_line else start_line
        if symbol:
            near = (start_line, end) if start_line is not None and end is not None else None
            commits = self.repo.log_symbol(path, symbol, k, near=near)
        if not commits and start_line is not None and end is not None:
            commits = self.repo.log_lines(path, start_line, end, k)
        if not commits and symbol is None and start_line is None:
            commits = list(
                self.repo.iter_commits(max_count=k, paths=[path], timeout=self.repo.timeout)
            )
        return self._commit_evidence(commits[:k], "history")

    def fetch(self, ref: str) -> Evidence | None:
        """Resolve a canonical ref to Evidence (see contracts.Toolbox.fetch). Malformed,
        unknown or unreachable refs give None."""
        try:
            kind, value = parse_ref(ref.strip())
        except (ValueError, AttributeError):
            return None
        if kind == "commit":
            return self._fetch_commit(value)
        if kind == "code":
            return self._fetch_code(value)
        if kind == "doc":
            chunk = self.index.get(f"doc:{value}")
            return self._chunk_evidence(chunk, "fetch") if chunk is not None else None
        if not _NUMBER.match(value):
            return None
        n = int(value)
        if kind == "pr":
            return self._fetch_pr(n)
        if kind == "issue":
            return self._fetch_issue(n)
        return self._fetch_gh(n)

    # -- lifecycle --------------------------------------------------------------------------

    def close(self) -> None:
        """Close the index and the GitHub client."""
        self.index.close()
        if self.github is not None:
            self.github.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- index chunks -----------------------------------------------------------------------

    def _chunk_evidence(self, c: Chunk, source: str, hit: Hit | None = None) -> Evidence:
        """Evidence for a code or doc chunk; meta carries path/start_line/end_line/symbol."""
        meta: dict[str, Any] = {
            **c.meta,
            "path": c.path,
            "start_line": c.start_line,
            "end_line": c.end_line,
            "symbol": c.symbol,
        }
        if hit is not None:
            meta.update(bm25_rank=hit.bm25_rank, dense_rank=hit.dense_rank)
        lines = (c.start_line, c.end_line) if c.kind == "code" else (None, None)
        return Evidence(
            id=c.id,
            kind=c.kind,
            title=c.title,
            text=c.text,
            url=self._blob_url(c.path, *lines),
            source=source,
            score=hit.score if hit is not None else 0.0,
            date=c.date,
            meta=meta,
        )

    def _commit_hits(self, hits: Sequence[Hit]) -> list[Evidence]:
        staged: list[tuple[Hit, str, str, list[str]]] = []
        for hit in hits:
            c = hit.chunk
            sha = str(c.meta.get("sha") or c.id.partition(":")[2])
            own = commit_ref(sha)
            message = _commit_chunk_message(c)
            refs = [r for r in extract_refs(message, self.owner, self.name) if not same_ref(r, own)]
            staged.append((hit, sha, c.title, refs))
        linked = self._link_prs([(sha, subject, refs) for _, sha, subject, refs in staged])
        out = []
        for (hit, sha, _subject, _refs), refs in zip(staged, linked, strict=True):
            c = hit.chunk
            meta = {k: v for k, v in c.meta.items() if k != "refs"}
            meta.update(bm25_rank=hit.bm25_rank, dense_rank=hit.dense_rank)
            out.append(
                Evidence(
                    id=c.id,
                    kind="commit",
                    title=c.title,
                    text=c.text,
                    url=self._commit_url(sha),
                    source="search:commits",
                    score=hit.score,
                    date=c.date,
                    refs=refs,
                    meta=meta,
                )
            )
        return out

    # -- commits ----------------------------------------------------------------------------

    def _commit_evidence(self, commits: Sequence[CommitInfo], source: str) -> list[Evidence]:
        items = [c.to_evidence(self.owner, self.name) for c in commits]
        linked = self._link_prs(
            [(c.sha, c.subject, e.refs) for c, e in zip(commits, items, strict=True)]
        )
        return [
            e.model_copy(update={"refs": refs, "source": source})
            for e, refs in zip(items, linked, strict=True)
        ]

    def _link_prs(self, commits: Sequence[tuple[str, str, Sequence[str]]]) -> list[list[str]]:
        """For (sha, subject, refs) of several commits, the refs with each commit's PR named:
        "Merge pull request #n" subjects give pr:n; a commit whose message names no PR gets
        the PRs GitHub associates with it (looked up concurrently, cached)."""
        staged: list[tuple[str, list[str], bool]] = []
        for sha, subject, refs in commits:
            out = list(refs)
            m = _MERGE_PR_SUBJECT.match(subject or "")
            if m:
                out = _put_first(out, pr_ref(int(m.group(1))))
            staged.append((sha, out, any(r.startswith("pr:") for r in out)))
        if self.github is None:
            return [out for _, out, _ in staged]
        missing = list(dict.fromkeys(sha for sha, _, has_pr in staged if not has_pr))
        with self._lock:
            todo = [sha for sha in missing if sha not in self._pr_cache]
        found: dict[str, list[str]] = {}  # this call's lookups, failures included
        if len(todo) > 1:
            with ThreadPoolExecutor(max_workers=min(_GITHUB_WORKERS, len(todo))) as pool:
                found = dict(zip(todo, pool.map(self._prs_for_commit, todo), strict=True))
        result = []
        for sha, out, has_pr in staged:
            if not has_pr:
                prs = found[sha] if sha in found else self._prs_for_commit(sha)
                for ref in reversed(prs):
                    out = _put_first(out, ref)
            result.append(out)
        return result

    def _prs_for_commit(self, sha: str) -> list[str]:
        with self._lock:
            cached = self._pr_cache.get(sha)
        if cached is not None:
            return cached
        prs = self._gh("prs_for_commit", lambda gh: gh.prs_for_commit(sha), None, quiet=True)
        if prs is None:  # failed: not cached, so a later call may succeed
            return []
        merged = [p for p in prs if p.merged]
        refs = [pr_ref(p.number) for p in (merged or prs)[:_MAX_PRS_PER_COMMIT]]
        with self._lock:
            self._pr_cache[sha] = refs
        return refs

    def _fetch_commit(self, value: str) -> Evidence | None:
        if not _SHA.match(value):
            return None
        info = self.repo.show(value)
        return self._commit_evidence([info], "fetch")[0] if info is not None else None

    # -- GitHub objects ---------------------------------------------------------------------

    def _search_github(self, query: str, k: int) -> list[Evidence]:
        if self.github is None:
            log.debug("prs search skipped: no GitHub client (offline)")
            return []
        found = self._gh("search", lambda gh: gh.search(query, k=k), None)
        out = []
        for item in found or []:
            e = self._github_evidence(item, "search:prs")
            # search hits carry no comments (and PRs no merge commit): fetch for the full item
            out.append(e.model_copy(update={"meta": {**e.meta, "partial": True}}))
        return out

    def _github_evidence(self, item: PullRequest | Issue, source: str) -> Evidence:
        return item.to_evidence(self.owner, self.name).model_copy(update={"source": source})

    def _fetch_pr(self, n: int) -> Evidence | None:
        pr = self._gh("get_pr", lambda gh: gh.get_pr(n), None)
        if pr is not None:
            return self._github_evidence(pr, "fetch")
        # "(#n)" is a PR by convention, but the number may belong to an issue
        issue = self._gh("get_issue", lambda gh: gh.get_issue(n), None)
        if issue is not None and not issue.is_pr:
            return self._github_evidence(issue, "fetch")
        return None

    def _fetch_issue(self, n: int) -> Evidence | None:
        issue = self._gh("get_issue", lambda gh: gh.get_issue(n), None)
        if issue is None:
            return None
        if issue.is_pr:  # PRs and issues share one number space: give the full PR
            pr = self._gh("get_pr", lambda gh: gh.get_pr(n), None)
            return self._github_evidence(pr if pr is not None else issue, "fetch")
        return self._github_evidence(issue, "fetch")

    def _fetch_gh(self, n: int) -> Evidence | None:
        item = self._gh("resolve", lambda gh: gh.resolve(n), None)
        return self._github_evidence(item, "fetch") if item is not None else None

    def _gh(
        self, what: str, call: Callable[[GitHubClient], T], default: T, *, quiet: bool = False
    ) -> T:
        """Run one GitHub call; `default` when offline, paused after a rate limit, or on a
        GitHub error (logged as a warning, or at debug level when `quiet`).

        Pauses are per rate-limit resource: GitHub budgets search separately from the rest
        of the REST API ("core"), so a spent search budget does not stop PR/issue fetches."""
        gh = self.github
        if gh is None:
            return default
        resource = "search" if what == "search" else "core"
        with self._lock:
            paused = time.time() < self._gh_paused_until.get(resource, 0.0)
        if paused:
            return default
        try:
            return call(gh)
        except RateLimited as exc:
            until = exc.reset_at or time.time() + _DEFAULT_RATE_LIMIT_PAUSE_S
            limited = getattr(exc, "resource", None) or resource
            with self._lock:
                self._gh_paused_until[limited] = max(self._gh_paused_until.get(limited, 0.0), until)
            log.warning("GitHub %s: %s; skipping GitHub calls (%s) until then", what, exc, limited)
        except _GITHUB_ERRORS as exc:
            log.log(logging.DEBUG if quiet else logging.WARNING, "GitHub %s failed: %s", what, exc)
        return default

    # -- code -------------------------------------------------------------------------------

    def _fetch_code(self, value: str) -> Evidence | None:
        m = _CODE_VALUE.match(value)
        if m is None:
            return None
        path = posixpath.normpath(m["path"].strip()).lstrip("/")
        if path in ("", ".") or path.startswith("../"):
            return None
        text = self.repo.read_file(path)
        # Same line breaks as the chunkers that made the index ids (not str.splitlines, which
        # also breaks on form feeds, \v, \x1c-\x1e, \x85, U+2028/2029 and shifts every line).
        lines = split_lines(text) if text is not None else []
        if not lines:
            return None
        start = int(m["a"]) if m["a"] else 1
        end = int(m["b"]) if m["b"] else (start if m["a"] else len(lines))
        start = max(1, start)
        if start > len(lines):
            return None
        end = min(max(start, end), len(lines), start + _MAX_FETCH_LINES - 1)
        ref = code_ref(path, start, end)
        chunk = self.index.get(ref)
        symbol = chunk.symbol if chunk is not None else None
        label = f" {symbol}" if symbol else ""
        body = "\n".join(lines[start - 1 : end])
        return Evidence(
            id=ref,
            kind="code",
            title=chunk.title if chunk is not None else f"{path} L{start}-{end}",
            text=f"{path}:{start}-{end}{label}\n{body}",
            url=self._blob_url(path, start, end),
            source="fetch",
            meta={"path": path, "start_line": start, "end_line": end, "symbol": symbol},
        )

    # -- links ------------------------------------------------------------------------------

    def _commit_url(self, sha: str) -> str | None:
        if self.owner and self.name:
            return f"https://github.com/{self.owner}/{self.name}/commit/{sha}"
        return None

    def _head_sha(self) -> str:
        with self._lock:
            head = self._head
        if head is None:
            head = self.index.get_meta("head_sha") or self.repo.head_sha() or "HEAD"
            with self._lock:
                self._head = head
        return head

    def _blob_url(
        self, path: str | None, start: int | None = None, end: int | None = None
    ) -> str | None:
        if not (self.owner and self.name and path):
            return None
        url = f"https://github.com/{self.owner}/{self.name}/blob/{self._head_sha()}/{path}"
        if start is not None:
            url += f"#L{start}-L{end if end is not None else start}"
        return url


# -- helpers -------------------------------------------------------------------------------


def _put_first(refs: Sequence[str], ref: str) -> list[str]:
    """`ref` first; an equivalent ref already present (e.g. gh:n for pr:n) is dropped."""
    return [ref, *(r for r in refs if not same_ref(r, ref))]


def _commit_chunk_message(chunk: Chunk) -> str:
    """Subject + body of an index commit chunk: its text minus the "commit <sha> by ..."
    header line and the trailing "files:" list (see index.chunkers.commit_chunk)."""
    text = chunk.text
    if text.startswith("commit "):
        text = text.partition("\n")[2]
    head, sep, _files = text.rpartition("\n\nfiles:\n")
    return head if sep else text


# -- construction --------------------------------------------------------------------------


def resolve_embedder(embedder: EmbedderChoice) -> Embedder | None:
    """ "default" -> FastEmbed when installed (else None: BM25-only); None -> BM25-only;
    an Embedder instance is used as is."""
    if isinstance(embedder, str):
        if embedder != "default":
            raise ValueError(f"embedder must be 'default', None or an Embedder, not {embedder!r}")
        return get_default_embedder()
    return embedder


def open_index(
    repo_path: str | Path,
    embedder: EmbedderChoice = "default",
    *,
    on_embedder_change: Literal["error", "rebuild"] = "error",
) -> HybridIndex:
    """The hybrid index of a repository, stored as index.sqlite in
    `config.repo_data_dir(repo_path)`. Raises `index.store.EmbedderMismatchError` when it was
    built with another embedder and `on_embedder_change` is "error"."""
    path = repo_data_dir(repo_path) / INDEX_FILE
    return HybridIndex(path, resolve_embedder(embedder), on_embedder_change=on_embedder_change)


def refresh_index(
    repo: GitRepo,
    index: HybridIndex,
    *,
    max_commits: int | None = None,
    force: bool = False,
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """`index.build.build_index` with the commit cap remembered in the index meta.

    `max_commits=None` reuses the cap of the previous build (default 5000), so refreshing an
    index built with `arch index --max-commits N` stays incremental instead of rebuilding
    with a different cap.
    """
    if max_commits is None:
        stored = index.get_meta(MAX_COMMITS_META)
        max_commits = int(stored) if stored and stored.isdigit() else DEFAULT_MAX_COMMITS
    stats = build_index(repo, index, max_commits=max_commits, force=force, progress=progress)
    index.set_meta(MAX_COMMITS_META, str(max_commits))
    return stats


def open_github(
    repo: GitRepo, *, token: str | None = None, max_wait_s: float = 10.0
) -> GitHubClient | None:
    """A GitHubClient for the clone's GitHub remote, caching responses in
    github_cache.sqlite in the repo data dir; None when the clone has no GitHub remote.
    Nothing is requested until a method is called."""
    slug = repo.remote_slug()
    if slug is None:
        return None
    owner, name = slug
    cache = repo_data_dir(repo.root) / GITHUB_CACHE_FILE
    return GitHubClient(owner, name, token=token, cache_path=cache, max_wait_s=max_wait_s)


def open_toolbox(
    repo_path: str | Path,
    offline: bool = False,
    build: bool = True,
    embedder: EmbedderChoice = "default",
    *,
    on_embedder_change: Literal["error", "rebuild"] = "error",
    github_token: str | None = None,
    github_max_wait_s: float = 10.0,
    progress: ProgressFn | None = None,
) -> RepoToolbox:
    """Open a RepoToolbox for the clone at `repo_path`.

    The index is index.sqlite in `config.repo_data_dir` (outside the clone); GitHub
    responses are cached in github_cache.sqlite next to it. `build=True` builds or
    incrementally refreshes the index first (a no-op when HEAD is unchanged) and keeps the
    stats in `toolbox.build_stats`. GitHub is used only when not `offline` and the clone has
    a GitHub remote; `github_max_wait_s` caps how long a rate-limited call may block.
    Raises ValueError if `repo_path` is not inside a git work tree.
    """
    repo = GitRepo(repo_path)
    if repo.is_shallow:
        log.warning(
            "%s is a shallow clone: history stops at its oldest fetched commit, which looks "
            "like it added every file; run `git fetch --unshallow` for the full history",
            repo.root,
        )
    index = open_index(repo.root, embedder, on_embedder_change=on_embedder_change)
    try:
        stats = refresh_index(repo, index, progress=progress) if build else None
        github = (
            None if offline else open_github(repo, token=github_token, max_wait_s=github_max_wait_s)
        )
    except BaseException:
        index.close()
        raise
    toolbox = RepoToolbox(repo, index, github)
    toolbox.build_stats = stats
    return toolbox
