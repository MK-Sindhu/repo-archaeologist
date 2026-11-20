"""Canonical references ("commit:<sha>", "pr:12", ...) and extraction from free text.

GitHub issues and PRs share one number space, so a bare "#12" becomes "gh:12" and is
resolved later by Toolbox.fetch. Squash-merge subjects ending in "(#12)" are PRs by
convention; "Fixes #7" style closing keywords point at issues.
"""

from __future__ import annotations

import re

_CLOSING = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_SHA_MIN = 7


def commit_ref(sha: str) -> str:
    return f"commit:{sha}"


def pr_ref(n: int) -> str:
    return f"pr:{int(n)}"


def issue_ref(n: int) -> str:
    return f"issue:{int(n)}"


def gh_ref(n: int) -> str:
    return f"gh:{int(n)}"


def code_ref(path: str, start: int, end: int) -> str:
    return f"code:{path}#L{start}-L{end}"


def doc_ref(path: str, slug: str) -> str:
    return f"doc:{path}#{slug}"


def parse_ref(ref: str) -> tuple[str, str]:
    """'pr:12' -> ('pr', '12'). Raises ValueError on malformed refs."""
    kind, sep, value = ref.partition(":")
    if not sep or not value or kind not in {"commit", "pr", "issue", "gh", "code", "doc"}:
        raise ValueError(f"not a canonical ref: {ref!r}")
    return kind, value


def same_ref(a: str, b: str) -> bool:
    """Equality that treats commit SHAs as prefix-matching (abbrev vs full) and gh:n as
    matching pr:n / issue:n."""
    try:
        ka, va = parse_ref(a)
        kb, vb = parse_ref(b)
    except ValueError:
        return a == b
    if ka == kb == "commit":
        short = min(len(va), len(vb))
        return short >= _SHA_MIN and va[:short].lower() == vb[:short].lower()
    numeric = {"pr", "issue", "gh"}
    if ka in numeric and kb in numeric and ("gh" in (ka, kb) or ka == kb):
        return va == vb
    return ka == kb and va == vb


def slugify(heading: str) -> str:
    s = re.sub(r"[^\w\s-]", "", heading.lower()).strip()
    return re.sub(r"[\s_]+", "-", s) or "section"


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def extract_refs(text: str, owner: str | None = None, repo: str | None = None) -> list[str]:
    """Find refs in commit messages / PR bodies / issue text, in order of appearance.

    - github.com/<o>/<r>/pull/12   -> pr:12      (only same repo when owner/repo given)
    - github.com/<o>/<r>/issues/7  -> issue:7
    - github.com/<o>/<r>/commit/<sha> -> commit:<sha>
    - "Fixes #7" / "closes #7"     -> issue:7
    - subject-style "(#12)"        -> pr:12
    - other "#12" / "GH-12"        -> gh:12
    - bare hex 7-40 chars containing a digit and a letter -> commit:<sha>
    """
    if not text:
        return []
    found: list[tuple[int, str]] = []
    consumed: list[tuple[int, int]] = []

    def take(m: re.Match, ref: str) -> None:
        found.append((m.start(), ref))
        consumed.append(m.span())

    def free(m: re.Match) -> bool:
        a, b = m.span()
        return all(b <= x or a >= y for x, y in consumed)

    url = re.compile(
        r"https?://github\.com/([\w.-]+)/([\w.-]+)/(pull|issues|commit)/([0-9a-fA-F]+|\d+)"
    )
    for m in url.finditer(text):
        o, r, typ, val = m.groups()
        if owner and repo and (o.lower() != owner.lower() or r.lower() != repo.lower()):
            consumed.append(m.span())
            continue
        if typ == "pull":
            take(m, pr_ref(int(val)))
        elif typ == "issues":
            take(m, issue_ref(int(val)))
        else:
            take(m, commit_ref(val.lower()))

    for m in re.finditer(rf"\b{_CLOSING}:?\s+#(\d+)\b", text, flags=re.IGNORECASE):
        if free(m):
            take(m, issue_ref(int(m.group(1))))

    for m in re.finditer(r"\(#(\d+)\)", text):
        if free(m):
            take(m, pr_ref(int(m.group(1))))

    for m in re.finditer(r"(?<![\w/])(?:#|GH-)(\d+)\b", text):
        if free(m):
            take(m, gh_ref(int(m.group(1))))

    for m in re.finditer(r"(?<![\w/#:-])([0-9a-f]{7,40})(?![\w-])", text):
        val = m.group(1)
        if free(m) and re.search(r"\d", val) and re.search(r"[a-f]", val):
            take(m, commit_ref(val))

    found.sort(key=lambda t: t[0])
    return _dedupe([r for _, r in found])
