"""GitHubClient tests. Every request goes to an httpx.MockTransport; no network, no tokens."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest

from archaeologist.sources import github as gh
from archaeologist.sources.github import (
    Comment,
    GitHubClient,
    GitHubError,
    Issue,
    PullRequest,
    RateLimited,
    resolve_token,
)

P = "/repos/acme/toy"
MERGED_AT = "2024-02-15T12:00:00Z"
Handler = Callable[[httpx.Request], httpx.Response]


class FakeGitHub:
    """Routes requests by URL path; records every request (thread-safe)."""

    def __init__(self) -> None:
        self.routes: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []
        self._lock = threading.Lock()

    def add(self, path: str, payload: Any = None, status: int = 200, **headers: str) -> None:
        self.routes[path] = lambda _req: httpx.Response(status, json=payload, headers=headers)

    def route(self, path: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.routes[path] = fn
            return fn

        return register

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return handler(request)

    def hits(self, path: str) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r.url.path == path)


def user(login: str, type_: str = "User") -> dict[str, str]:
    return {"login": login, "type": type_}


def pr_payload(n: int, title: str = "Change", body: str = "", merged: bool = True, **kw) -> dict:
    data = {
        "number": n,
        "title": title,
        "body": body,
        "state": "closed" if merged else "open",
        "merged_at": MERGED_AT if merged else None,
        "merge_commit_sha": f"{n:040x}",
        "html_url": f"https://github.com/acme/toy/pull/{n}",
        "user": user("alice"),
        "labels": [{"name": "bug"}],
        "created_at": "2024-02-10T09:00:00Z",
        "base": {"ref": "main"},
    }
    data.update(kw)
    return data


def issue_payload(n: int, title: str = "Problem", body: str = "", is_pr: bool = False, **kw):
    data = {
        "number": n,
        "title": title,
        "body": body,
        "state": "closed",
        "html_url": f"https://github.com/acme/toy/{'pull' if is_pr else 'issues'}/{n}",
        "user": user("erin"),
        "created_at": "2024-01-20T08:00:00Z",
        "closed_at": "2024-02-15T12:00:00Z",
        "labels": [],
    }
    if is_pr:
        data["pull_request"] = {"url": f"https://api.github.com{P}/pulls/{n}", "merged_at": None}
    data.update(kw)
    return data


def comment(login: str, body: str, at: str, type_: str = "User") -> dict:
    return {"user": user(login, type_), "body": body, "created_at": at}


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    """Keep the developer's real GitHub credentials out of every test."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    real_run = subprocess.run

    def run(args, *a, **kw):
        if args and args[0] == "gh":
            raise FileNotFoundError("gh")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(gh.time, "sleep", calls.append)
    return calls


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


def client(fake: FakeGitHub, **kw) -> GitHubClient:
    kw.setdefault("token", "test-token")
    return GitHubClient("acme", "toy", transport=httpx.MockTransport(fake), **kw)


def add_pr_12(fake: FakeGitHub, merge_sha: str) -> None:
    body = (
        "<!-- Please describe your change -->\n"
        "Fixes #7. Users reported multi-minute sleeps. Related to #9, see "
        "https://github.com/acme/toy/issues/9 (not https://github.com/other/x/pull/3)."
    )
    fake.add(
        f"{P}/pulls/12",
        pr_payload(
            12,
            "Cap backoff at 30s to avoid huge sleeps (#12)",
            body,
            merge_commit_sha=merge_sha,
            comments=3,
            review_comments=1,
        ),
    )
    fake.add(
        f"{P}/issues/12/comments",
        [
            comment("bob", "Why 30s? We discussed this in #7.", "2024-02-11T10:00:00Z"),
            comment("codecov[bot]", "Coverage up. See #99", "2024-02-11T11:00:00Z", "Bot"),
            comment("carol", "LGTM, relates to #15.", "2024-02-13T10:00:00Z"),
        ],
    )
    fake.add(
        f"{P}/pulls/12/comments",
        [comment("dave", "nit: name the constant MAX_BACKOFF", "2024-02-12T10:00:00Z")],
    )


# --------------------------------------------------------------------------- PRs


def test_prs_for_commit_puts_merging_pr_first(fake, toy_repo_shas):
    _, shas = toy_repo_shas
    sha = shas[1]  # "Cap backoff at 30s to avoid huge sleeps (#12)"
    fake.add(
        f"{P}/commits/{sha}/pulls",
        [
            pr_payload(20, "Unrelated open PR", merged=False),
            pr_payload(12, "Cap backoff", "Fixes #7.", merge_commit_sha=sha),
        ],
    )
    prs = client(fake).prs_for_commit(sha)
    assert [p.number for p in prs] == [12, 20]
    top = prs[0]
    assert top.merged and top.merge_commit_sha == sha
    assert top.linked_issues == [7]
    assert top.base_ref == "main" and top.labels == ["bug"] and top.user == "alice"
    assert not prs[1].merged


def test_prs_for_unknown_commit_is_empty(fake):
    fake.add(f"{P}/commits/abc1234/pulls", {"message": "No commit found for SHA"}, status=422)
    assert client(fake).prs_for_commit("abc1234") == []
    assert client(fake).prs_for_commit("def5678") == []  # 404


def test_get_pr_with_comments_and_evidence(fake):
    sha = "3f2a9c1d" * 5
    add_pr_12(fake, sha)
    pr = client(fake).get_pr(12)
    assert isinstance(pr, PullRequest)
    assert pr.linked_issues == [7, 9]
    assert [(c.author, c.kind) for c in pr.comments] == [
        ("bob", "issue"),
        ("dave", "review"),
        ("carol", "issue"),
    ]  # chronological, bot comment dropped

    e = pr.to_evidence("acme", "toy")
    assert e.id == "pr:12" and e.kind == "pr" and e.source == "github"
    assert e.refs == ["issue:7", "issue:9", "gh:15", f"commit:{sha}"]
    assert e.date == MERGED_AT
    assert e.url == "https://github.com/acme/toy/pull/12"
    assert "Fixes #7" in e.text and "Please describe" not in e.text
    assert "Discussion:" in e.text
    assert "- @bob: Why 30s?" in e.text and "- @dave: nit:" in e.text
    assert "codecov" not in e.text
    assert e.meta["merged"] is True and e.meta["labels"] == ["bug"]


def test_get_pr_without_comments_skips_comment_endpoints(fake):
    add_pr_12(fake, "a" * 40)
    pr = client(fake).get_pr(12, with_comments=False)
    assert pr is not None and pr.comments == []
    assert fake.hits(f"{P}/issues/12/comments") == 0
    assert fake.hits(f"{P}/pulls/12/comments") == 0


def test_get_pr_skips_comment_requests_when_counts_are_zero(fake):
    fake.add(f"{P}/pulls/5", pr_payload(5, comments=0, review_comments=0))
    assert client(fake).get_pr(5) is not None
    assert [r.url.path for r in fake.requests] == [f"{P}/pulls/5"]


def test_unmerged_pr_evidence_has_no_merge_commit(fake):
    fake.add(f"{P}/pulls/20", pr_payload(20, "WIP", "See #4", merged=False, comments=0))
    e = client(fake).get_pr(20).to_evidence("acme", "toy")
    assert e.refs == ["gh:4"]
    assert e.date == "2024-02-10T09:00:00Z"
    assert e.text.endswith("\n\nSee #4") and "not merged" in e.text


def test_bot_author_detection():
    assert PullRequest.from_api(pr_payload(1, user=user("dependabot[bot]"))).user_is_bot
    assert PullRequest.from_api(pr_payload(2, user=user("renovate", "Bot"))).user_is_bot
    assert not PullRequest.from_api(pr_payload(3)).user_is_bot
    assert PullRequest.from_api(pr_payload(4, user=None)).user == "ghost"


def test_pr_files_paginates_and_keeps_missing_patch(fake):
    page2 = f"https://api.github.com{P}/pulls/12/files?per_page=100&page=2"

    @fake.route(f"{P}/pulls/12/files")
    def files(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[{"filename": "logo.png", "status": "added"}])
        return httpx.Response(
            200,
            json=[{"filename": "src/pkg/retry.py", "status": "modified", "patch": "@@ -1 +1 @@"}],
            headers={"Link": f'<{page2}>; rel="next"'},
        )

    files_ = client(fake).pr_files(12)
    assert [(f.filename, f.status, f.patch) for f in files_] == [
        ("src/pkg/retry.py", "modified", "@@ -1 +1 @@"),
        ("logo.png", "added", None),
    ]
    assert client(fake).pr_files(404) == []


# --------------------------------------------------------------------------- issues


def test_get_issue_404_is_none(fake):
    assert client(fake).get_issue(404) is None


def test_get_issue_with_comments_and_evidence(fake):
    fake.add(
        f"{P}/issues/7",
        issue_payload(7, "Backoff sleeps for minutes", "Seen on v1.2 (#7). Maybe #3?", comments=1),
    )
    fake.add(
        f"{P}/issues/7/comments",
        [comment("bob", "Fixed by https://github.com/acme/toy/pull/12", "2024-02-15T12:00:00Z")],
    )
    issue = client(fake).get_issue(7)
    assert isinstance(issue, Issue) and not issue.is_pr
    assert issue.comments == [
        Comment(
            author="bob",
            body="Fixed by https://github.com/acme/toy/pull/12",
            created_at="2024-02-15T12:00:00Z",
            kind="issue",
        )
    ]
    e = issue.to_evidence("acme", "toy")
    assert e.id == "issue:7" and e.kind == "issue"
    assert e.refs == ["gh:3", "pr:12"]  # own "(#7)" dropped
    assert e.date == "2024-01-20T08:00:00Z"
    assert e.meta["closed_at"] == "2024-02-15T12:00:00Z"


def test_issue_payload_with_pull_request_is_pr(fake):
    fake.add(f"{P}/issues/12", issue_payload(12, "Cap backoff", is_pr=True, comments=0))
    issue = client(fake).get_issue(12)
    assert issue is not None and issue.is_pr
    e = issue.to_evidence()
    assert e.id == "pr:12" and e.kind == "pr"


def test_resolve_issue_and_pr(fake):
    add_pr_12(fake, "b" * 40)
    fake.add(f"{P}/issues/12", issue_payload(12, is_pr=True))
    fake.add(f"{P}/issues/7", issue_payload(7, "Backoff sleeps", comments=1))
    fake.add(f"{P}/issues/7/comments", [comment("bob", "+1", "2024-01-21T00:00:00Z")])
    c = client(fake)

    issue = c.resolve(7)
    assert isinstance(issue, Issue) and issue.number == 7
    assert [x.author for x in issue.comments] == ["bob"]
    assert fake.hits(f"{P}/pulls/7") == 0

    pr = c.resolve(12)
    assert isinstance(pr, PullRequest) and pr.number == 12 and pr.merged
    assert len(pr.comments) == 3

    assert c.resolve(999) is None


# --------------------------------------------------------------------------- search


def test_search_scopes_query_and_merges_by_score(fake):
    @fake.route("/search/issues")
    def search(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        assert q.startswith("backoff cap repo:acme/toy ")
        if q.endswith("is:pr"):
            item = issue_payload(12, "Cap backoff", is_pr=True, score=5.0)
            item["pull_request"]["merged_at"] = MERGED_AT
            items = [item, issue_payload(15, "Add jitter", is_pr=True, score=1.0)]
        else:
            assert q.endswith("is:issue")
            items = [issue_payload(7, "Sleeps too long", score=3.0)]
        return httpx.Response(200, json={"total_count": len(items), "items": items})

    c = client(fake)
    results = c.search("  backoff\ncap ", k=10)
    assert [(type(r).__name__, r.number) for r in results] == [
        ("PullRequest", 12),
        ("Issue", 7),
        ("PullRequest", 15),
    ]
    assert results[0].merged_at == MERGED_AT and results[0].merge_commit_sha is None
    assert results[0].url == "https://github.com/acme/toy/pull/12"
    assert [r.number for r in c.search("backoff cap", k=2)] == [12, 7]

    fake.requests.clear()
    only_prs = c.search("backoff cap", k=5, kind="pr")
    assert all(isinstance(r, PullRequest) for r in only_prs)
    issues = c.search("backoff cap", k=5, kind="issue")
    assert [r.number for r in issues] == [7]


def test_search_edge_cases(fake):
    fake.add("/search/issues", {"total_count": 0, "items": []})
    c = client(fake)
    assert c.search("   ") == []
    assert c.search("x", k=0) == []
    assert fake.requests == []
    with pytest.raises(ValueError):
        c.search("x", kind="commits")  # type: ignore[arg-type]

    c.search("word " * 100, kind="pr")
    q = fake.requests[-1].url.params["q"]
    free_text = q.removesuffix(" repo:acme/toy is:pr")
    assert len(free_text) <= 256 and free_text.endswith("word")
    assert fake.requests[-1].url.params["per_page"] == "10"

    c.search("jitter repo:other/x is:issue org:evil herd", kind="pr")
    assert fake.requests[-1].url.params["q"] == "jitter herd repo:acme/toy is:pr"
    assert c.search("repo:other/x is:open") == []


def test_non_object_payload_is_github_error(fake):
    fake.add(f"{P}/pulls/1", [{"number": 1}])
    with pytest.raises(GitHubError):
        client(fake).get_pr(1)


# --------------------------------------------------------------------------- pagination


def test_list_merged_prs_follows_link_pagination(fake):
    page2 = f"https://api.github.com{P}/pulls?state=closed&page=2"

    @fake.route(f"{P}/pulls")
    def pulls(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=[pr_payload(3), pr_payload(2)])
        assert request.url.params["state"] == "closed"
        assert request.url.params["base"] == "main"
        return httpx.Response(
            200,
            json=[pr_payload(6), pr_payload(5, merged=False), pr_payload(4)],
            headers={"Link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
        )

    c = client(fake)
    assert [p.number for p in c.list_merged_prs(limit=10, base="main")] == [6, 4, 3, 2]
    assert fake.hits(f"{P}/pulls") == 2

    fresh = FakeGitHub()
    fresh.routes = fake.routes
    assert [p.number for p in client(fresh).list_merged_prs(limit=2, base="main")] == [6, 4]
    assert fresh.hits(f"{P}/pulls") == 1  # stopped before fetching page 2
    assert client(fresh).list_merged_prs(limit=0) == []


# --------------------------------------------------------------------------- cache


def test_cache_hit_makes_no_second_http_call(fake):
    add_pr_12(fake, "c" * 40)
    c = client(fake)
    first = c.get_pr(12)
    n_requests = len(fake.requests)
    assert c.get_pr(12) == first
    assert len(fake.requests) == n_requests == 3


def test_file_cache_survives_new_client(fake, tmp_path: Path):
    add_pr_12(fake, "d" * 40)
    db = tmp_path / "cache" / "github.sqlite"
    with client(fake, cache_path=db) as c:
        first = c.get_pr(12)
    n_requests = len(fake.requests)
    with client(fake, cache_path=db) as c:
        assert c.get_pr(12) == first
    assert len(fake.requests) == n_requests
    assert db.exists()


def test_etag_revalidation_304_serves_cached_body(fake):
    @fake.route(f"{P}/issues/7")
    def issue(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"ETag": '"v1"'})
        return httpx.Response(
            200, json=issue_payload(7, "Original", comments=0), headers={"ETag": '"v1"'}
        )

    c = client(fake, cache_ttl_s=0)
    assert c.get_issue(7).title == "Original"
    assert "if-none-match" not in fake.requests[0].headers
    assert c.get_issue(7).title == "Original"
    assert fake.requests[1].headers["if-none-match"] == '"v1"'
    assert fake.hits(f"{P}/issues/7") == 2


def test_etag_change_replaces_cached_body(fake):
    version = {"etag": '"v1"', "title": "Original"}

    @fake.route(f"{P}/issues/7")
    def issue(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == version["etag"]:
            return httpx.Response(304)
        payload = issue_payload(7, version["title"], comments=0)
        return httpx.Response(200, json=payload, headers={"ETag": version["etag"]})

    c = client(fake, cache_ttl_s=0)
    assert c.get_issue(7).title == "Original"
    version.update(etag='"v2"', title="Edited")
    assert c.get_issue(7).title == "Edited"
    assert c.get_issue(7).title == "Edited"
    assert fake.requests[-1].headers["if-none-match"] == '"v2"'


# --------------------------------------------------------------------------- errors


def test_rate_limit_with_no_wait_allowed_raises(fake, sleeps):
    reset = 4_000_000_000
    fake.add(
        f"{P}/issues/7",
        {"message": "API rate limit exceeded"},
        status=403,
        **{"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
    )
    with pytest.raises(RateLimited) as info:
        client(fake, max_wait_s=0).get_issue(7)
    assert info.value.reset_at == reset
    assert info.value.status == 403
    assert sleeps == []
    assert "test-token" not in str(info.value)


def test_retry_after_429_raises_when_wait_too_long(fake, sleeps):
    fake.add(f"{P}/issues/7", {"message": "slow down"}, status=429, **{"Retry-After": "30"})
    with pytest.raises(RateLimited) as info:
        client(fake, max_wait_s=0).get_issue(7)
    assert info.value.reset_at is not None
    assert sleeps == []


def test_rate_limit_waits_then_retries_once(fake, sleeps):
    calls = {"n": 0}

    @fake.route(f"{P}/issues/7")
    def issue(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "3"})
        return httpx.Response(200, json=issue_payload(7, comments=0))

    assert client(fake, max_wait_s=10).get_issue(7).number == 7
    assert sleeps == [3.0]

    fake.add(f"{P}/issues/8", {"message": "slow down"}, status=429, **{"Retry-After": "1"})
    with pytest.raises(RateLimited):
        client(fake, max_wait_s=10).get_issue(8)
    assert fake.hits(f"{P}/issues/8") == 2  # one retry, then give up


def test_secondary_rate_limit_message_without_headers(fake, sleeps):
    fake.add(f"{P}/issues/7", {"message": "You have exceeded a secondary rate limit."}, status=403)
    with pytest.raises(RateLimited):
        client(fake, max_wait_s=0).get_issue(7)


def test_plain_403_is_github_error_not_rate_limit(fake, sleeps):
    fake.add(f"{P}/issues/7", {"message": "Resource not accessible"}, status=403)
    with pytest.raises(GitHubError) as info:
        client(fake).get_issue(7)
    assert not isinstance(info.value, RateLimited)
    assert info.value.status == 403
    assert "Resource not accessible" in str(info.value)
    assert sleeps == []


def test_410_is_none_and_missing_list_is_empty(fake):
    fake.add(f"{P}/issues/7", {"message": "Gone"}, status=410)
    c = client(fake)
    assert c.get_issue(7) is None
    assert c.list_merged_prs() == []


def test_5xx_retried_twice_then_succeeds(fake, sleeps):
    calls = {"n": 0}

    @fake.route(f"{P}/issues/7")
    def issue(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(502)
        return httpx.Response(200, json=issue_payload(7, comments=0))

    assert client(fake).get_issue(7).number == 7
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]


def test_5xx_persistent_raises_after_two_retries(fake, sleeps):
    fake.add(f"{P}/issues/7", {"message": "unavailable"}, status=503)
    with pytest.raises(GitHubError) as info:
        client(fake).get_issue(7)
    assert info.value.status == 503
    assert fake.hits(f"{P}/issues/7") == 3


def test_network_error_is_github_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    c = GitHubClient("acme", "toy", token="t", transport=httpx.MockTransport(boom))
    with pytest.raises(GitHubError):
        c.get_pr(1)


def test_invalid_json_is_github_error():
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy login</html>")

    c = GitHubClient("acme", "toy", token="t", transport=httpx.MockTransport(html))
    with pytest.raises(GitHubError):
        c.get_issue(1)


# --------------------------------------------------------------------------- auth + headers


def test_request_headers(fake):
    fake.add(f"{P}/issues/7", issue_payload(7, comments=0))
    client(fake, token="explicit-token").get_issue(7)
    headers = fake.requests[0].headers
    assert headers["user-agent"] == "repo-archaeologist"
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["authorization"] == "Bearer explicit-token"


def test_unauthenticated_sends_no_authorization(fake):
    fake.add(f"{P}/issues/7", issue_payload(7, comments=0))
    c = GitHubClient("acme", "toy", transport=httpx.MockTransport(fake))
    assert not c.authenticated
    c.get_issue(7)
    assert "authorization" not in fake.requests[0].headers


def test_token_resolution_order(monkeypatch):
    gh_calls: list[list[str]] = []

    def fake_run(args, *a, **kw):
        assert kw.get("timeout") is not None and kw["timeout"] <= 10
        gh_calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="gho_from_cli\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "from_github_token")
    monkeypatch.setenv("GH_TOKEN", "from_gh_token")
    assert resolve_token("explicit") == "explicit"
    assert resolve_token() == "from_github_token"
    monkeypatch.setenv("GITHUB_TOKEN", "  ")
    assert resolve_token() == "from_gh_token"
    monkeypatch.delenv("GITHUB_TOKEN")
    monkeypatch.delenv("GH_TOKEN")
    assert gh_calls == []
    assert resolve_token() == "gho_from_cli"
    assert gh_calls[0][:3] == ["gh", "auth", "token"]


@pytest.mark.parametrize(
    "outcome",
    [
        FileNotFoundError("gh"),
        subprocess.TimeoutExpired("gh", 5),
        subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="not logged in"),
        subprocess.CompletedProcess(["gh"], 0, stdout="\n", stderr=""),
    ],
)
def test_gh_cli_failures_mean_unauthenticated(monkeypatch, outcome):
    def fake_run(args, *a, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_token() is None


def test_client_uses_resolved_env_token(fake, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "env-token")
    fake.add(f"{P}/issues/7", issue_payload(7, comments=0))
    c = GitHubClient("acme", "toy", transport=httpx.MockTransport(fake))
    assert c.authenticated
    c.get_issue(7)
    assert fake.requests[0].headers["authorization"] == "Bearer env-token"


# --------------------------------------------------------------------------- text + threads


def test_evidence_text_is_capped_with_digest():
    comments = [
        Comment(author=f"user{i}", body=f"comment {i} " + "y" * 1000, created_at=f"2024-01-{i:02d}")
        for i in range(1, 41)
    ]
    pr = PullRequest(number=1, title="Big", body="x" * 10_000, comments=comments)
    text = pr.to_evidence().text
    assert len(text) <= 6100
    assert "…[truncated]" in text
    assert "- @user1: comment 1 " in text
    assert "more comments)" in text
    digest_lines = [line for line in text.splitlines() if line.startswith("- @")]
    assert digest_lines and all(len(line) <= 420 for line in digest_lines)


def test_evidence_short_body_keeps_everything():
    pr = PullRequest(
        number=2,
        title="Small",
        body="Short body.",
        comments=[Comment(author="a", body="first\n\nreply", created_at="2024-01-01")],
    )
    status = "Status: open, not merged yet.\n\n"
    assert pr.to_evidence().text == status + "Short body.\n\nDiscussion:\n- @a: first reply"
    assert PullRequest(number=3, title="Empty").to_evidence().text == status + "(no description)"


def test_pr_evidence_says_whether_it_shipped():
    merged = PullRequest(number=4, title="Fix", state="closed", merged_at="2026-03-02T10:00:00Z")
    rejected = PullRequest(number=5, title="AI junk", state="closed", body="Same fix, again.")
    assert merged.to_evidence().text.startswith("Status: merged on 2026-03-02.")
    ev = rejected.to_evidence()
    assert ev.text.startswith("Status: closed WITHOUT being merged. This change did not ship.")
    assert ev.meta["merged"] is False and ev.meta["state"] == "closed"


def test_client_is_thread_safe(fake, tmp_path: Path):
    for n in range(1, 21):
        fake.add(f"{P}/pulls/{n}", pr_payload(n, f"PR {n}", f"Fixes #{n + 100}", comments=1))
        fake.add(f"{P}/issues/{n}/comments", [comment("bob", f"re {n}", "2024-01-01T00:00:00Z")])
    c = client(fake, cache_path=tmp_path / "gh.sqlite")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(c.get_pr, [n for n in range(1, 21) for _ in range(3)]))
    assert [r.number for r in results] == [n for n in range(1, 21) for _ in range(3)]
    assert all(r.linked_issues == [r.number + 100] for r in results)
    assert all(r.comments[0].body == f"re {r.number}" for r in results)
    c.close()


@pytest.mark.live
def test_live_public_repo(tmp_path: Path):
    with GitHubClient("pallets", "click", cache_path=tmp_path / "gh.sqlite", max_wait_s=0) as c:
        first = c.resolve(1)
        assert first is not None and first.number == 1
        assert c.prs_for_commit("0000000") == []
        hits = c.search("progress bar", k=3, kind="pr")
        assert hits and all(isinstance(h, PullRequest) for h in hits)
