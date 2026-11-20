"""A fake GitHub API for the `toy_repo` fixture (see conftest.py), served to a real
GitHubClient through httpx.MockTransport. Imported by test_toolbox.py and test_e2e.py.

The toy history it describes:

  c1  "Add retry helper"            -> GitHub associates it with merged PR #3
  c2  "Cap backoff ... (#12)"       -> PR #12 (merge commit c2), closes issue #7
  c3  "Add jitter to backoff (#15)" -> PR #15, mentions issue #9
  c4  "docs: describe retry settings" -> no PR
"""

from __future__ import annotations

import re
import threading
from typing import Any

import httpx

from archaeologist.sources.github import GitHubClient

P = "/repos/acme/toy"
MERGED_AT = "2024-02-15T12:00:00Z"

PR12_BODY = (
    "<!-- Thanks for contributing! -->\n"
    "Fixes #7.\n\n"
    "Users reported multi-minute sleeps: `2 ** attempt` grows without bound, so after eight "
    "failures a worker waits more than four minutes before its next try and looks hung. "
    "This caps the delay at `MAX_BACKOFF = 30` seconds, which matches the upstream gateway "
    "timeout, so a retry never waits longer than the request it is retrying."
)
PR12_PATCH = '''@@ -1,7 +1,9 @@
 """Retry helpers."""

+MAX_BACKOFF = 30
+

 def backoff(attempt):
     """Seconds to wait before retry number `attempt`."""
-    return 2 ** attempt
+    return min(2 ** attempt, MAX_BACKOFF)'''


def user(login: str, type_: str = "User") -> dict[str, str]:
    return {"login": login, "type": type_}


def comment(login: str, body: str, at: str) -> dict[str, Any]:
    return {"user": user(login), "body": body, "created_at": at}


def pr_payload(
    n: int, title: str, body: str, merge_sha: str | None, merged: bool = True, **kw: Any
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": n,
        "title": title,
        "body": body,
        "state": "closed" if merged else "open",
        "merged_at": MERGED_AT if merged else None,
        "merge_commit_sha": merge_sha,
        "html_url": f"https://github.com/acme/toy/pull/{n}",
        "user": user("alice"),
        "labels": [],
        "created_at": "2024-02-10T09:00:00Z",
        "base": {"ref": "main"},
        "comments": 0,
        "review_comments": 0,
    }
    data.update(kw)
    return data


def issue_payload(n: int, title: str, body: str, *, pr: bool = False, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": n,
        "title": title,
        "body": body,
        "state": "closed",
        "html_url": f"https://github.com/acme/toy/{'pull' if pr else 'issues'}/{n}",
        "user": user("erin"),
        "created_at": "2024-01-20T08:00:00Z",
        "closed_at": "2024-02-15T12:00:00Z",
        "labels": [],
        "comments": 0,
    }
    if pr:
        data["pull_request"] = {
            "url": f"https://api.github.com{P}/pulls/{n}",
            "merged_at": MERGED_AT,
        }
    data.update(kw)
    return data


class ToyGitHub:
    """Routes GitHub REST requests for acme/toy; records every request (thread-safe).
    `fail` maps a URL path to a status code, or (status, headers), to answer with instead
    (error injection)."""

    def __init__(self, shas: list[str]):
        c1, c2, c3, c4 = shas
        self.shas = shas
        self.requests: list[httpx.Request] = []
        self.fail: dict[str, int | tuple[int, dict[str, str]]] = {}
        self._lock = threading.Lock()

        pr3 = pr_payload(3, "Add retry helper", "First version of the retry helper.", c1)
        pr12 = pr_payload(12, "Cap backoff at 30s", PR12_BODY, c2, comments=1, review_comments=1)
        pr15 = pr_payload(15, "Add jitter to backoff", "Thundering herd. See #9.", c3)
        self.prs = {3: pr3, 12: pr12, 15: pr15}
        self.issues = {
            7: issue_payload(
                7,
                "Retries sleep for minutes",
                "After a few failures backoff() returns 256+ seconds and our workers look hung.",
                comments=1,
            ),
            9: issue_payload(9, "Thundering herd on retry", "All clients retry at once."),
        }
        for n, pr in self.prs.items():
            self.issues[n] = issue_payload(n, pr["title"], pr["body"], pr=True)
        self.routes: dict[str, Any] = {
            f"{P}/commits/{c1}/pulls": [pr3],
            f"{P}/commits/{c2}/pulls": [pr12],
            f"{P}/commits/{c3}/pulls": [pr15],
            f"{P}/commits/{c4}/pulls": [],
            f"{P}/pulls": [pr15, pr12, pr3],
            f"{P}/pulls/12/files": [
                {"filename": "src/pkg/retry.py", "status": "modified", "patch": PR12_PATCH}
            ],
            f"{P}/issues/12/comments": [
                comment("bob", "30s matches the gateway timeout, so that is the cap.", MERGED_AT)
            ],
            f"{P}/pulls/12/comments": [
                comment("carol", "Should the cap be configurable later?", "2024-02-12T10:00:00Z")
            ],
            f"{P}/issues/7/comments": [
                comment("dave", "Same here; a cap around 30 seconds would be fine.", MERGED_AT)
            ],
        }
        for n, pr in self.prs.items():
            self.routes[f"{P}/pulls/{n}"] = pr
        for n, issue in self.issues.items():
            self.routes[f"{P}/issues/{n}"] = issue

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        path = request.url.path
        if path in self.fail:
            failure = self.fail[path]
            status, headers = failure if isinstance(failure, tuple) else (failure, {})
            return httpx.Response(status, json={"message": "Injected failure"}, headers=headers)
        if path == "/search/issues":
            return httpx.Response(200, json=self._search(request.url.params.get("q", "")))
        if path in self.routes:
            return httpx.Response(200, json=self.routes[path])
        return httpx.Response(404, json={"message": "Not Found"})

    def _search(self, q: str) -> dict[str, Any]:
        """Items of the requested kind (is:pr / is:issue) sharing a word with the query."""
        words = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in {"is", "pr", "issue"}}
        words -= {"repo", "acme", "toy"}
        want_pr = "is:pr" in q
        items = []
        pool = [i for n, i in self.issues.items() if ("pull_request" in i) == want_pr]
        for item in pool:
            text = f"{item['title']} {item['body']}".lower()
            hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}\b", text))
            if hits:
                items.append({**item, "score": float(hits)})
        items.sort(key=lambda i: -i["score"])
        return {"total_count": len(items), "incomplete_results": False, "items": items}

    def hits(self, path: str) -> int:
        with self._lock:
            return sum(1 for r in self.requests if r.url.path == path)

    def client(self, **kw: Any) -> GitHubClient:
        """A real GitHubClient talking to this fake (in-memory cache, explicit token)."""
        kw.setdefault("token", "test-token")
        return GitHubClient("acme", "toy", transport=httpx.MockTransport(self), **kw)
