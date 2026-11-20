"""GitHub REST client (httpx) with a SQLite response cache.

`GitHubClient` fetches the PRs, issues and discussions that explain a change. Every GET is
cached by URL together with its ETag: fresh entries are served without touching the network,
stale ones are revalidated with `If-None-Match` (a 304 does not count against the rate limit).

Failure policy: 404/410 mean "not there" (None or []); rate limits wait up to `max_wait_s`
and retry once, otherwise raise `RateLimited`; 5xx are retried twice with backoff; anything
else (including network errors) raises `GitHubError`. Callers such as the toolbox decide how
to degrade, so the agent never crashes on GitHub trouble.

The client is safe to share between threads: httpx.Client is thread-safe and the SQLite
cache serialises access behind a lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, NamedTuple, Self

import httpx
from pydantic import BaseModel, Field

from ..models import Evidence
from ..refs import commit_ref, extract_refs, gh_ref, issue_ref, parse_ref, pr_ref, same_ref

log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
USER_AGENT = "repo-archaeologist"
DEFAULT_CACHE_TTL_S = 24 * 3600.0

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_GH_CLI_TIMEOUT_S = 5.0
_RETRY_BACKOFF_S = (1.0, 2.0)  # sleeps before the two 5xx retries
_SECONDARY_LIMIT_WAIT_S = 60.0  # GitHub's advice when a rate limit names no reset time
_MISSING = frozenset({404, 410})
_MAX_COMMENTS = 100  # per endpoint (issue comments, review comments)
_MAX_SEARCH_QUERY = 256  # GitHub's limit on free text in a search query
_MAX_TEXT = 6000  # Evidence.text budget: body + discussion digest
_MAX_COMMENT_CHARS = 400
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SCOPE_QUALIFIER = re.compile(r"(?<!\S)(?:repo|org|user|is):\S*", re.IGNORECASE)

SearchKind = Literal["both", "pr", "issue"]


class GitHubError(RuntimeError):
    """A GitHub request failed (network error, unexpected status, malformed payload)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimited(GitHubError):
    """GitHub rate limit hit and the reset is further away than the client may wait.

    `reset_at` is the Unix time at which requests should succeed again (None if unknown).
    `resource` is the limit that was hit: GitHub keeps separate budgets per resource ("core"
    for the REST API, "search" for /search, ...), so callers can keep using the others.
    """

    def __init__(
        self, reset_at: float | None, status: int | None = None, resource: str | None = None
    ):
        when = datetime.fromtimestamp(reset_at, UTC).isoformat() if reset_at else "unknown"
        which = f" ({resource})" if resource else ""
        super().__init__(f"GitHub rate limit{which} exceeded; resets at {when}", status=status)
        self.reset_at = reset_at
        self.resource = resource


def rate_limit_resource(path: str) -> str:
    """The rate-limit bucket GitHub charges a request path to ("search" or "core")."""
    return "search" if path.startswith("/search/") else "core"


# --------------------------------------------------------------------------- models


class Comment(BaseModel):
    """One discussion comment: a conversation ("issue") comment or an inline review comment."""

    author: str
    body: str
    created_at: str | None = None
    kind: Literal["issue", "review"] = "issue"

    @classmethod
    def from_api(cls, data: dict[str, Any], kind: Literal["issue", "review"]) -> Comment:
        """Build from a GitHub comment payload."""
        return cls(
            author=_login(data.get("user")),
            body=data.get("body") or "",
            created_at=data.get("created_at"),
            kind=kind,
        )


class PullRequest(BaseModel):
    """A pull request. Search results carry no merge commit / base branch: those stay None."""

    number: int
    title: str
    body: str = ""
    state: str = "open"
    merged_at: str | None = None
    merge_commit_sha: str | None = None
    url: str = ""
    user: str = "ghost"
    user_is_bot: bool = False
    labels: list[str] = Field(default_factory=list)
    created_at: str | None = None
    base_ref: str | None = None
    comments: list[Comment] = Field(default_factory=list)
    linked_issues: list[int] = Field(default_factory=list)

    @property
    def merged(self) -> bool:
        """True when the PR was merged."""
        return self.merged_at is not None

    @classmethod
    def from_api(
        cls,
        data: dict[str, Any],
        owner: str | None = None,
        repo: str | None = None,
        comments: list[Comment] | None = None,
    ) -> PullRequest:
        """Build from a pulls payload or an issue-shaped PR (search results, /issues/{n})."""
        number = int(data["number"])
        body = data.get("body") or ""
        linked = [
            int(parse_ref(ref)[1])
            for ref in extract_refs(body, owner, repo)
            if ref.startswith("issue:") and ref != issue_ref(number)
        ]
        return cls(
            number=number,
            title=data.get("title") or "",
            body=body,
            state=data.get("state") or "open",
            merged_at=data.get("merged_at") or (data.get("pull_request") or {}).get("merged_at"),
            merge_commit_sha=data.get("merge_commit_sha"),
            url=data.get("html_url") or "",
            user=_login(data.get("user")),
            user_is_bot=_is_bot(data.get("user")),
            labels=_labels(data),
            created_at=data.get("created_at"),
            base_ref=(data.get("base") or {}).get("ref"),
            comments=comments or [],
            linked_issues=linked,
        )

    def to_evidence(self, owner: str | None = None, repo: str | None = None) -> Evidence:
        """Evidence id `pr:<n>`; refs = linked issues, mentions, then the merge commit."""
        own = pr_ref(self.number)
        refs = [issue_ref(n) for n in self.linked_issues]
        refs += _mentioned_refs(self.title, self.body, self.comments, owner, repo)
        if self.merged and self.merge_commit_sha:
            refs.append(commit_ref(self.merge_commit_sha))
        return Evidence(
            id=own,
            kind="pr",
            title=self.title,
            text=_compose_text(self.body, self.comments),
            url=self.url or None,
            source="github",
            date=self.merged_at or self.created_at,
            refs=_merge_refs(refs, self.number),
            meta={
                "number": self.number,
                "state": self.state,
                "merged": self.merged,
                "author": self.user,
                "author_is_bot": self.user_is_bot,
                "labels": self.labels,
                "base_ref": self.base_ref,
            },
        )


class Issue(BaseModel):
    """An issue. GitHub serves PRs from the issues API too; those have `is_pr=True`."""

    number: int
    title: str
    body: str = ""
    state: str = "open"
    url: str = ""
    user: str = "ghost"
    created_at: str | None = None
    closed_at: str | None = None
    comments: list[Comment] = Field(default_factory=list)
    is_pr: bool = False

    @classmethod
    def from_api(cls, data: dict[str, Any], comments: list[Comment] | None = None) -> Issue:
        """Build from an issues payload."""
        return cls(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            state=data.get("state") or "open",
            url=data.get("html_url") or "",
            user=_login(data.get("user")),
            created_at=data.get("created_at"),
            closed_at=data.get("closed_at"),
            comments=comments or [],
            is_pr="pull_request" in data,
        )

    def to_evidence(self, owner: str | None = None, repo: str | None = None) -> Evidence:
        """Evidence id `issue:<n>` (`pr:<n>` when the item is really a PR); refs = mentions."""
        own = pr_ref(self.number) if self.is_pr else issue_ref(self.number)
        return Evidence(
            id=own,
            kind="pr" if self.is_pr else "issue",
            title=self.title,
            text=_compose_text(self.body, self.comments),
            url=self.url or None,
            source="github",
            date=self.created_at,
            refs=_merge_refs(
                _mentioned_refs(self.title, self.body, self.comments, owner, repo), self.number
            ),
            meta={
                "number": self.number,
                "state": self.state,
                "author": self.user,
                "closed_at": self.closed_at,
            },
        )


class PRFile(BaseModel):
    """A file changed by a PR. `patch` is None for binary or very large diffs."""

    filename: str
    status: str
    patch: str | None = None


# --------------------------------------------------------------------------- token


def resolve_token(token: str | None = None) -> str | None:
    """GitHub token from, in order: `token`, $GITHUB_TOKEN, $GH_TOKEN, `gh auth token`.

    Returns None (unauthenticated) when none is available. The token is never logged.
    """
    for candidate in (token, os.getenv("GITHUB_TOKEN"), os.getenv("GH_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    return _gh_cli_token()


def _gh_cli_token() -> str | None:
    try:
        proc = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=_GH_CLI_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (proc.stdout or "").strip()
    return out if proc.returncode == 0 and out else None


# --------------------------------------------------------------------------- cache


class _CacheEntry(NamedTuple):
    etag: str | None
    next_url: str | None
    body: str
    fetched_at: float


class _ResponseCache:
    """URL -> (etag, next page URL, JSON body). One connection shared across threads."""

    def __init__(self, path: str | Path | None):
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            ":memory:" if path is None else str(path), check_same_thread=False, timeout=30
        )
        with self._lock:
            if path is not None:
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS http_cache ("
                "url TEXT PRIMARY KEY, etag TEXT, next_url TEXT, body TEXT NOT NULL, "
                "fetched_at REAL NOT NULL)"
            )
            self._db.commit()

    def get(self, url: str) -> _CacheEntry | None:
        with self._lock:
            row = self._db.execute(
                "SELECT etag, next_url, body, fetched_at FROM http_cache WHERE url = ?", (url,)
            ).fetchone()
        return _CacheEntry(*row) if row else None

    def put(self, url: str, etag: str | None, next_url: str | None, body: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache (url, etag, next_url, body, fetched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (url, etag, next_url, body, time.time()),
            )
            self._db.commit()

    def touch(self, url: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE http_cache SET fetched_at = ? WHERE url = ?", (time.time(), url)
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


class _Page(NamedTuple):
    data: Any
    next_url: str | None


# --------------------------------------------------------------------------- client


class GitHubClient:
    """Read-only client for one repository's PRs and issues.

    `cache_path=None` keeps the cache in memory. Cache entries younger than `cache_ttl_s`
    are served without a request; older ones are revalidated with their ETag
    (`cache_ttl_s=0` always revalidates, None never expires). `max_wait_s` caps how long a
    rate-limited request may sleep before its single retry.
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        token: str | None = None,
        cache_path: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        max_wait_s: float = 60.0,
        cache_ttl_s: float | None = DEFAULT_CACHE_TTL_S,
        base_url: str = API_URL,
    ):
        self.owner = owner
        self.repo = repo
        self.max_wait_s = max_wait_s
        self.cache_ttl_s = cache_ttl_s
        resolved = resolve_token(token)
        self.authenticated = resolved is not None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if resolved:
            headers["Authorization"] = f"Bearer {resolved}"
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=_TIMEOUT,
            transport=transport,
            # renamed repos answer with 301; transferred issues too (rejected in _numbered)
            follow_redirects=True,
        )
        self._cache = _ResponseCache(cache_path)
        self._prefix = f"/repos/{owner}/{repo}"

    # -- public API -----------------------------------------------------------------

    def prs_for_commit(self, sha: str) -> list[PullRequest]:
        """PRs associated with a commit, the PR that merged it first.

        Unknown commits (e.g. never pushed) give [].
        """
        items = self._paginate(
            f"{self._prefix}/commits/{sha}/pulls",
            {"per_page": 100},
            missing=_MISSING | {422},
        )
        prs = [PullRequest.from_api(d, self.owner, self.repo) for d in items]
        sha = sha.lower()
        return sorted(
            prs,
            key=lambda p: (
                not (p.merge_commit_sha or "").lower().startswith(sha),
                not p.merged,
            ),
        )

    def get_pr(self, n: int, with_comments: bool = True) -> PullRequest | None:
        """A PR with its conversation and review comments (chronological), or None."""
        data = self._numbered(f"{self._prefix}/pulls/{int(n)}", n)
        if data is None:
            return None
        comments: list[Comment] = []
        if with_comments:
            if data.get("comments", 1):
                comments += self._comments(f"{self._prefix}/issues/{int(n)}/comments", "issue")
            if data.get("review_comments", 1):
                comments += self._comments(f"{self._prefix}/pulls/{int(n)}/comments", "review")
            comments.sort(key=lambda c: c.created_at or "")
        return PullRequest.from_api(data, self.owner, self.repo, comments)

    def get_issue(self, n: int, with_comments: bool = True) -> Issue | None:
        """An issue with its comments, or None. PR numbers come back with `is_pr=True`.
        An issue transferred to another repository is None (see `_numbered`)."""
        data = self._numbered(f"{self._prefix}/issues/{int(n)}", n)
        return None if data is None else self._issue(data, with_comments)

    def resolve(self, n: int) -> PullRequest | Issue | None:
        """Resolve a `gh:<n>` number: the PR if n is a PR, else the issue, else None."""
        data = self._numbered(f"{self._prefix}/issues/{int(n)}", n)
        if data is None:
            return None
        if "pull_request" in data:
            return self.get_pr(n)
        return self._issue(data, with_comments=True)

    def search(
        self, query: str, k: int = 10, kind: SearchKind = "both"
    ) -> list[PullRequest | Issue]:
        """Full-text search over this repo's PRs and/or issues, best match first.

        Results carry no comments and (for PRs) no merge commit or base branch.
        `kind="both"` runs one PR and one issue search and merges them by score. Scope
        qualifiers in `query` (repo:, org:, user:, is:) are dropped so results stay in this repo.
        """
        if kind not in ("both", "pr", "issue"):
            raise ValueError(f"kind must be 'both', 'pr' or 'issue', not {kind!r}")
        text = _clip_words(" ".join(_SCOPE_QUALIFIER.sub(" ", query).split()), _MAX_SEARCH_QUERY)
        if not text or k <= 0:
            return []
        kinds = ("pr", "issue") if kind == "both" else (kind,)
        scored: list[tuple[float, PullRequest | Issue]] = []
        for qualifier in kinds:
            q = f"{text} repo:{self.owner}/{self.repo} is:{qualifier}"
            data = self._get_object("/search/issues", {"q": q, "per_page": min(k, 100)})
            for item in (data or {}).get("items") or []:
                result: PullRequest | Issue = (
                    PullRequest.from_api(item, self.owner, self.repo)
                    if "pull_request" in item
                    else Issue.from_api(item)
                )
                scored.append((float(item.get("score") or 0.0), result))
        scored.sort(key=lambda t: -t[0])
        return [r for _, r in scored[:k]]

    def list_merged_prs(self, limit: int = 200, base: str | None = None) -> list[PullRequest]:
        """Up to `limit` merged PRs, most recently created first (follows pagination, skips
        unmerged ones). `base` restricts to PRs into that branch."""
        params: dict[str, Any] = {
            "state": "closed",
            "sort": "created",
            "direction": "desc",
            "per_page": 100,
        }
        if base:
            params["base"] = base
        out: list[PullRequest] = []
        if limit <= 0:
            return out
        for data in self._paginate(f"{self._prefix}/pulls", params):
            if data.get("merged_at"):
                out.append(PullRequest.from_api(data, self.owner, self.repo))
                if len(out) >= limit:
                    break
        return out

    def pr_files(self, n: int, limit: int = 300) -> list[PRFile]:
        """Files changed by PR `n` (up to `limit`); [] if the PR does not exist."""
        out: list[PRFile] = []
        if limit <= 0:
            return out
        for data in self._paginate(f"{self._prefix}/pulls/{int(n)}/files", {"per_page": 100}):
            out.append(
                PRFile(
                    filename=data.get("filename") or "",
                    status=data.get("status") or "modified",
                    patch=data.get("patch"),
                )
            )
            if len(out) >= limit:
                break
        return out

    def close(self) -> None:
        """Close the HTTP connection pool and the cache database."""
        self._http.close()
        self._cache.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- internals ------------------------------------------------------------------

    def _issue(self, data: dict[str, Any], with_comments: bool) -> Issue:
        comments = []
        if with_comments and data.get("comments", 1):
            comments = self._comments(
                f"{self._prefix}/issues/{int(data['number'])}/comments", "issue"
            )
        return Issue.from_api(data, comments)

    def _comments(self, path: str, kind: Literal["issue", "review"]) -> list[Comment]:
        out = []
        for data in self._paginate(path, {"per_page": _MAX_COMMENTS}):
            if not _is_bot(data.get("user")) and (data.get("body") or "").strip():
                out.append(Comment.from_api(data, kind))
            if len(out) >= _MAX_COMMENTS:
                break
        return out

    def _numbered(self, path: str, n: int) -> dict[str, Any] | None:
        """The issue/PR object at `path`, or None if missing or not item #n of this repo.

        Redirects are followed (renamed repositories answer 301), but GitHub also answers 301
        for an issue transferred to another repository, and that issue has the other
        repository's number: its number and refs would be misread as this repository's. The
        check is on the payload, so it also holds for responses served from the cache."""
        data = self._get_object(path)
        if data is None:
            return None
        got = data.get("number")
        if got is not None and str(got) != str(int(n)):
            log.info(
                "GitHub #%d answered as #%s (transferred to another repository?): ignored",
                int(n),
                got,
            )
            return None
        return data

    def _get_object(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        page = self._get(path, params)
        if page is None:
            return None
        if not isinstance(page.data, dict):
            raise GitHubError(f"expected a JSON object from {path}")
        return page.data

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None, *, missing: frozenset[int] = _MISSING
    ) -> Iterator[dict[str, Any]]:
        """Yield items across pages, following Link: rel="next" lazily."""
        url: str | None = path
        while url:
            page = self._get(url, params, missing=missing)
            if page is None:
                return
            if not isinstance(page.data, list):
                raise GitHubError(f"expected a JSON list from {url}")
            yield from page.data
            url, params = page.next_url, None

    def _get(
        self, url: str, params: dict[str, Any] | None = None, *, missing: frozenset[int] = _MISSING
    ) -> _Page | None:
        """Cached GET. Returns None for `missing` statuses; raises GitHubError otherwise."""
        request = self._http.build_request("GET", url, params=params)
        key = str(request.url)
        cached = self._cache.get(key)
        if cached and (
            self.cache_ttl_s is None or time.time() - cached.fetched_at < self.cache_ttl_s
        ):
            return _Page(_loads(cached.body, key), cached.next_url)
        if cached and cached.etag:
            request.headers["If-None-Match"] = cached.etag
        resp = self._send(request)
        status = resp.status_code
        if status == 304 and cached:
            self._cache.touch(key)
            return _Page(_loads(cached.body, key), cached.next_url)
        if status in missing:
            return None
        if 200 <= status < 300:
            data = _loads(resp.text, key)
            next_url = resp.links.get("next", {}).get("url")
            self._cache.put(key, resp.headers.get("etag"), next_url, resp.text)
            return _Page(data, next_url)
        raise GitHubError(f"GET {key} -> {status}: {_message(resp)}", status=status)

    def _send(self, request: httpx.Request) -> httpx.Response:
        """Send with one rate-limit retry (if the wait fits) and two 5xx retries."""
        rate_retried = False
        server_retries = 0
        while True:
            try:
                resp = self._http.send(request)
            except httpx.HTTPError as exc:
                raise GitHubError(f"GET {request.url} failed: {exc}") from exc
            if resp.status_code in (403, 429):
                limit = _rate_limit_wait(resp, time.time())
                if limit is not None:
                    wait, reset_at = limit
                    if rate_retried or wait > self.max_wait_s:
                        resource = resp.headers.get("x-ratelimit-resource") or (
                            rate_limit_resource(request.url.path)
                        )
                        raise RateLimited(reset_at, status=resp.status_code, resource=resource)
                    log.info("GitHub rate limit hit; waiting %.0fs before retrying", wait)
                    time.sleep(wait)
                    rate_retried = True
                    continue
            if resp.status_code >= 500 and server_retries < len(_RETRY_BACKOFF_S):
                log.debug("GitHub %s for %s; retrying", resp.status_code, request.url)
                time.sleep(_RETRY_BACKOFF_S[server_retries])
                server_retries += 1
                continue
            return resp


# --------------------------------------------------------------------------- helpers


def _login(user: dict[str, Any] | None) -> str:
    return (user or {}).get("login") or "ghost"


def _is_bot(user: dict[str, Any] | None) -> bool:
    user = user or {}
    return user.get("type") == "Bot" or (user.get("login") or "").endswith("[bot]")


def _labels(data: dict[str, Any]) -> list[str]:
    out = []
    for label in data.get("labels") or []:
        name = label.get("name") if isinstance(label, dict) else label
        if name:
            out.append(str(name))
    return out


def _loads(text: str, url: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise GitHubError(f"invalid JSON from {url}") from exc


def _message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    return str(data.get("message", "")) if isinstance(data, dict) else ""


def _rate_limit_wait(resp: httpx.Response, now: float) -> tuple[float, float] | None:
    """(seconds to wait, reset Unix time) if `resp` is a rate-limit rejection, else None."""
    retry_after = resp.headers.get("retry-after")
    if retry_after is not None:
        try:
            wait = max(0.0, float(retry_after))
        except ValueError:
            wait = _SECONDARY_LIMIT_WAIT_S
        return wait, now + wait
    if resp.headers.get("x-ratelimit-remaining") == "0":
        try:
            reset_at = float(resp.headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return _SECONDARY_LIMIT_WAIT_S, now + _SECONDARY_LIMIT_WAIT_S
        return max(0.0, reset_at - now) + 1.0, reset_at  # +1s for clock skew
    if "rate limit" in _message(resp).lower():
        return _SECONDARY_LIMIT_WAIT_S, now + _SECONDARY_LIMIT_WAIT_S
    return None


def _clip_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    clipped = text[: limit + 1].rsplit(" ", 1)[0]
    return clipped if clipped and len(clipped) <= limit else text[:limit]


def _clean(text: str | None) -> str:
    """Drop HTML comments (PR/issue template boilerplate) and surrounding whitespace."""
    return _HTML_COMMENT.sub("", text or "").strip()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n…[truncated]"


def _compose_text(body: str, comments: list[Comment]) -> str:
    """Body plus a one-line-per-comment digest, capped near _MAX_TEXT characters.

    The digest may claim at most half the budget when both parts are long.
    """
    body = _clean(body)
    lines = []
    for c in comments:
        snippet = " ".join(_clean(c.body).split())
        if snippet:
            if len(snippet) > _MAX_COMMENT_CHARS:
                snippet = snippet[:_MAX_COMMENT_CHARS].rstrip() + "…"
            lines.append(f"- @{c.author}: {snippet}")
    if not lines:
        return _truncate(body, _MAX_TEXT) or "(no description)"
    header = "Discussion:"
    digest_len = len(header) + sum(len(line) + 1 for line in lines)
    body_budget = max(_MAX_TEXT - digest_len, _MAX_TEXT // 2)
    body = _truncate(body, body_budget)
    budget = _MAX_TEXT - len(body) - len(header) - 2
    kept: list[str] = []
    for line in lines:
        if len(line) + 1 > budget:
            break
        kept.append(line)
        budget -= len(line) + 1
    if len(kept) < len(lines):
        kept.append(f"- …({len(lines) - len(kept)} more comments)")
    digest = "\n".join([header, *kept])
    return f"{body}\n\n{digest}" if body else digest


def _mentioned_refs(
    title: str, body: str, comments: list[Comment], owner: str | None, repo: str | None
) -> list[str]:
    refs = extract_refs(title, owner, repo) + extract_refs(body, owner, repo)
    for c in comments:
        refs += extract_refs(c.body, owner, repo)
    return refs


def _merge_refs(refs: Iterable[str], number: int) -> list[str]:
    """De-duplicate refs (gh:n == pr:n/issue:n, SHA prefixes), keeping the most specific
    form at the first position it appeared, and drop refs to the item itself.

    PRs and issues share one number space, so pr:n, issue:n and gh:n with the item's own
    number all point back at it (e.g. an issue quoting a squash subject "(#n)")."""
    own = {pr_ref(number), issue_ref(number), gh_ref(number)}
    out: list[str] = []
    for ref in refs:
        if ref in own:
            continue
        for i, existing in enumerate(out):
            if same_ref(ref, existing):
                if ref != existing and _more_specific(ref, existing):
                    out[i] = ref
                break
        else:
            out.append(ref)
    return out


def _more_specific(a: str, b: str) -> bool:
    ka, va = parse_ref(a)
    kb, vb = parse_ref(b)
    if ka == kb == "commit":
        return len(va) > len(vb)
    return kb == "gh" and ka != "gh"
