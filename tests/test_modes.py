"""Answer modes: what/where/how questions are explained from the code as it is now (no link
following), why questions dig through history, and repositories with too little history are
always explained (and skip the PR search). FakeLLM + FakeToolbox only."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from archaeologist.config import GraphConfig
from archaeologist.graph import prompts
from archaeologist.graph.graph import ask
from archaeologist.graph.nodes import THIN_HISTORY_COMMITS
from archaeologist.testing import FakeLLM, FakeToolbox, ev
from archaeologist.toolbox import RepoToolbox

EVIDENCE_IDS = re.compile(r'<evidence id="([^"]+)">')
CODE = "code:src/pkg/retry.py#L8-L13"
COMMIT = "commit:c6d98fc00bcb20f09cbca555c1c69b297ad532cc"

# sha256 of the why-mode system prompts as committed before explain mode existed (a50594e):
# benchmark results stay comparable only while these are byte-identical.
WHY_PROMPT_HASHES = {
    "SYNTHESIZE_SYSTEM": "8d08312a2ca1a8f6f96d21f843763780eacab7941f25a7be9a448a603945aa12",
    "GRADE_SYSTEM": "a160f1a7ffb471f49032845f5f627d7ec66c16723e7067a3becc008384d8e701",
    "ANALYZE_SYSTEM": "001ac42d3745d68da6af523a25111948248e1f998b6c7ee953196fd6e620e38c",
    "VERIFY_SYSTEM": "851bb4cd76363d3da6f991bce167ff132467497a37a562104f9e0885c9c19bef",
}


@pytest.mark.parametrize("name", sorted(WHY_PROMPT_HASHES))
def test_why_mode_prompts_are_unchanged(name):
    text = getattr(prompts, name)
    assert hashlib.sha256(text.encode()).hexdigest() == WHY_PROMPT_HASHES[name]


class Recorder:
    """A scripted LLM that records which system prompt each step used."""

    def __init__(self, qtype: str, follow: list[str] | None = None):
        self.systems: dict[str, list[str]] = {}
        self.prompts: dict[str, list[str]] = {}
        self.qtype = qtype
        self.follow = follow or []

    def _seen(self, name: str, system: str, prompt: str) -> None:
        self.systems.setdefault(name, []).append(system)
        self.prompts.setdefault(name, []).append(prompt)

    def llm(self) -> FakeLLM:
        def analysis(system: str, prompt: str) -> dict[str, Any]:
            self._seen("Analysis", system, prompt)
            return {
                "question_type": self.qtype,
                "targets": [],
                "queries": [
                    {"source": "code", "query": "backoff"},
                    {"source": "commits", "query": "cap backoff"},
                    {"source": "prs", "query": "backoff cap"},
                    {"source": "docs", "query": "retry"},
                ],
            }

        def grades(system: str, prompt: str) -> dict[str, Any]:
            self._seen("Grades", system, prompt)
            ids = EVIDENCE_IDS.findall(prompt)
            return {"grades": [{"id": i, "score": 3, "reason": "t"} for i in ids], "follow": self.follow}

        def draft(system: str, prompt: str) -> dict[str, Any]:
            self._seen("Draft", system, prompt)
            return {"answer": f"It caps the delay [{CODE}].", "cited_ids": [CODE], "confidence": "high", "missing": []}

        def verification(system: str, prompt: str) -> dict[str, Any]:
            self._seen("Verification", system, prompt)
            return {"claims": [{"claim": "caps", "cited_ids": [CODE], "supported": True}], "follow_up_queries": []}

        return FakeLLM({"Analysis": analysis, "Grades": grades, "Draft": draft, "Verification": verification})


def toolbox(commits: int | None = None) -> FakeToolbox:
    tb = FakeToolbox(
        search_results={
            "code": [ev(CODE, kind="code", title="backoff", meta={"path": "src/pkg/retry.py"})],
            "commits": [ev(COMMIT, title="Cap backoff (#12)", refs=["pr:12"])],
            "prs": [ev("pr:15", kind="pr", title="Add jitter")],
        },
        fetchable={"pr:12": ev("pr:12", kind="pr", title="Cap backoff", refs=["issue:7"])},
    )
    if commits is not None:
        tb.commit_count = lambda: commits  # type: ignore[attr-defined]
    return tb


def searched(tb: FakeToolbox) -> list[str]:
    return [args[0] for kind, args in tb.calls if kind == "search"]


def fetched(tb: FakeToolbox) -> list[str]:
    return [args[0] for kind, args in tb.calls if kind == "fetch"]


@pytest.mark.parametrize("qtype", ["what", "where", "how"])
def test_explain_questions_use_explain_mode_and_follow_nothing(qtype):
    rec, tb = Recorder(qtype, follow=["pr:12"]), toolbox()
    answer = ask("How does backoff work?", tb, rec.llm(), GraphConfig())
    assert answer.mode == "explain"
    assert set(rec.systems["Grades"]) == {prompts.GRADE_EXPLAIN_SYSTEM}
    assert rec.systems["Draft"] == [prompts.SYNTHESIZE_EXPLAIN_SYSTEM]
    assert fetched(tb) == [] and answer.hops == 0  # no hops, though the grader asked
    assert "prs" in searched(tb)  # history exists: PRs are still searched
    assert any(line.startswith("analyze: ") and "explain mode" in line for line in answer.trace)


@pytest.mark.parametrize("qtype", ["why", "when", "who"])
def test_why_questions_dig_and_follow_links(qtype):
    rec, tb = Recorder(qtype, follow=["pr:12"]), toolbox(commits=250)
    answer = ask("Why is backoff capped?", tb, rec.llm(), GraphConfig())
    assert answer.mode == "why"
    assert set(rec.systems["Grades"]) == {prompts.GRADE_SYSTEM}
    assert rec.systems["Draft"] == [prompts.SYNTHESIZE_SYSTEM]
    assert "pr:12" in fetched(tb) and answer.hops >= 1
    assert "Note:" not in rec.prompts["Draft"][0]


@pytest.mark.parametrize("commits", [1, THIN_HISTORY_COMMITS])
def test_thin_history_forces_explain_mode_without_pr_search(commits):
    rec, tb = Recorder("why", follow=["pr:12"]), toolbox(commits=commits)
    answer = ask("Why is backoff capped?", tb, rec.llm(), GraphConfig())
    assert answer.mode == "explain"
    assert "prs" not in searched(tb) and {"code", "commits", "docs"} <= set(searched(tb))
    assert "prs" not in rec.prompts["Analysis"][0]
    assert fetched(tb) == []
    noun = "commit" if commits == 1 else "commits"
    assert f"Note: This repository's history has {commits} {noun}; do not infer motives" in rec.prompts["Draft"][0]
    assert rec.systems["Draft"] == [prompts.SYNTHESIZE_EXPLAIN_SYSTEM]
    assert any(f"thin history ({commits} {noun})" in line for line in answer.trace)
    verify_prompt = rec.prompts["Verification"][0]
    assert "Sources for follow_up_queries: code, commits, docs" in verify_prompt


def test_more_history_than_the_threshold_is_not_thin():
    rec, tb = Recorder("why"), toolbox(commits=THIN_HISTORY_COMMITS + 1)
    answer = ask("Why is backoff capped?", tb, rec.llm(), GraphConfig())
    assert answer.mode == "why" and "prs" in searched(tb)


def test_a_failing_commit_count_is_not_thin():
    rec, tb = Recorder("why"), toolbox()

    def boom() -> int:
        raise RuntimeError("index closed")

    tb.commit_count = boom  # type: ignore[attr-defined]
    assert ask("Why is backoff capped?", tb, rec.llm(), GraphConfig()).mode == "why"


def test_repo_toolbox_counts_commits_from_its_index(toy_repo, monkeypatch):
    monkeypatch.setenv("ARCH_EMBEDDER", "none")
    from archaeologist.toolbox import open_toolbox

    with open_toolbox(toy_repo, offline=True) as tb:
        assert isinstance(tb, RepoToolbox) and tb.commit_count() == 4


def test_unmerged_closed_prs_are_capped_whatever_the_grader_says():
    """Seen live on pallets/markupsafe: a rejected duplicate PR ("AI junk") graded 3."""
    from archaeologist.config import GraphConfig, LLMConfig
    from archaeologist.graph.nodes import UNMERGED_PR_MAX, AgentNodes
    from archaeologist.testing import FakeLLM, FakeToolbox, ev

    rejected = ev("pr:529", kind="pr", title="AI junk", meta={"merged": False, "state": "closed"})
    merged = ev("pr:522", kind="pr", title="fix striptags", meta={"merged": True, "state": "closed"})
    still_open = ev("pr:600", kind="pr", title="wip", meta={"merged": False, "state": "open"})
    llm = FakeLLM({"Grades": lambda s, p: {"grades": [
        {"id": "pr:529", "score": 3, "reason": "explains it"},
        {"id": "pr:522", "score": 3, "reason": "explains it"},
        {"id": "pr:600", "score": 2, "reason": "related"},
    ], "follow": []}})
    nodes = AgentNodes(FakeToolbox(), llm, GraphConfig(), LLMConfig())
    batch = [(e, e.render()) for e in (rejected, merged, still_open)]
    grades, _ = nodes._grade_batch("why?", "why", batch)
    assert grades["pr:529"].score == UNMERGED_PR_MAX == 1
    assert "closed without merging" in grades["pr:529"].reason
    assert grades["pr:522"].score == 3 and grades["pr:600"].score == 2
