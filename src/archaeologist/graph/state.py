"""Agent state and its reducers.

Channels written by parallel `retrieve` tasks (and by repeated rounds) use reducers so
concurrent updates merge instead of clobbering each other:

- `evidence`  merged by id: a full item replaces a partial one (a PR/issue search hit, which
              has no discussion), otherwise the item with the higher score is kept
- `grades`    merged by id (later grades win; a None value removes the grade, so an item
              whose partial version was graded is graded again once fetched in full)
- `trace`     appended
- `visited`, `traced`, `searched`, `suggested`   lists with set semantics (order kept)

Every other channel is last-value and written by exactly one node per step.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from ..models import Answer, Evidence
from .schemas import Analysis, Draft, Grade, SourceQuery

PARTIAL_SOURCES = ("search:prs",)  # PR/issue search hits: no comments, no merge commit


def is_partial(item: Evidence) -> bool:
    """True for a PR/issue known only from a search hit (meta["partial"], or produced by the
    "prs" search): fetching it gives the discussion and links the search result lacks."""
    if item.kind not in ("pr", "issue"):
        return False
    return bool(item.meta.get("partial")) or item.source in PARTIAL_SOURCES


def merge_evidence(
    left: dict[str, Evidence] | None, right: dict[str, Evidence] | None
) -> dict[str, Evidence]:
    """Merge evidence pools by id. A full item replaces a partial one (and is never replaced
    by one); otherwise the higher-scored item is kept (ties keep the existing one)."""
    merged = dict(left or {})
    for eid, item in (right or {}).items():
        current = merged.get(eid)
        if current is None:
            merged[eid] = item
            continue
        current_partial, item_partial = is_partial(current), is_partial(item)
        if current_partial != item_partial:
            if current_partial:
                merged[eid] = item
        elif item.score > current.score:
            merged[eid] = item
    return merged


def merge_grades(
    left: dict[str, Grade] | None, right: dict[str, Grade | None] | None
) -> dict[str, Grade]:
    """Merge grade maps by evidence id; the newer grade wins and None removes a grade."""
    merged = dict(left or {})
    for eid, grade in (right or {}).items():
        if grade is None:
            merged.pop(eid, None)
        else:
            merged[eid] = grade
    return merged


def add_unique(left: list[str] | None, right: list[str] | None) -> list[str]:
    """Append `right` to `left`, skipping values already present (a list with set semantics)."""
    out = list(left or [])
    seen = set(out)
    for value in right or []:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


class AgentState(TypedDict, total=False):
    """Graph state. Only `question` is required as input."""

    question: str
    started_at: float  # wall-clock start, set by analyze
    usage_start: dict[str, int]  # llm.usage_snapshot() before the first LLM call
    analysis: Analysis
    queries: list[SourceQuery]  # the next retrieve fan-out (analyze / verify retry)
    searched: Annotated[list[str], add_unique]  # "source\x1fquery" keys already run
    evidence: Annotated[dict[str, Evidence], merge_evidence]  # everything retrieved
    grades: Annotated[dict[str, Grade], merge_grades]  # every item graded so far (None: regrade)
    kept: list[str]  # ids with score >= 2, best first, capped at max_evidence
    suggested: Annotated[list[str], add_unique]  # refs the grader asked to follow
    frontier: list[str]  # refs to fetch on the next hop
    visited: Annotated[list[str], add_unique]  # refs already fetched (or attempted)
    traced: Annotated[list[str], add_unique]  # code locations whose history was taken
    hops: int
    retries: int  # verify-triggered retrieval rounds used
    draft: Draft
    unsupported: list[str]  # claims the latest verification could not support
    answer: Answer
    trace: Annotated[list[str], operator.add]


class RetrieveTask(TypedDict):
    """Payload of one `Send("retrieve", ...)` fan-out task."""

    source: str
    query: str
