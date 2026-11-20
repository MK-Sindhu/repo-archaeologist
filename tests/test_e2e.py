"""End to end: the LangGraph agent over a real RepoToolbox (toy repo + hybrid index + a real
GitHubClient on a fake GitHub), and smoke tests of the `arch` CLI with a scripted FakeLLM.
No network, no API keys."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from typing import Any

import anthropic
import httpx
import pytest
from toy_github import ToyGitHub
from typer.testing import CliRunner

from archaeologist import cli
from archaeologist.config import GraphConfig
from archaeologist.eval.mine import load_benchmark
from archaeologist.graph import ask
from archaeologist.index.build import build_index
from archaeologist.index.store import HybridIndex
from archaeologist.models import Answer, BenchmarkItem
from archaeologist.sources.git import GitRepo
from archaeologist.testing import FakeLLM, HashEmbedder
from archaeologist.toolbox import RepoToolbox, open_toolbox

QUESTION = "Why is backoff capped at 30 seconds?"
BACKOFF_ID = "code:src/pkg/retry.py#L8-L13"
EVIDENCE_IDS = re.compile(r'<evidence id="([^"]+)">')
RENDERED_IDS = re.compile(r"^\[([^\]\s]+)\] \(", re.MULTILINE)  # Evidence.render() heads
AUTH_MESSAGE = (
    "Could not resolve authentication method. Expected one of api_key, auth_token, or "
    "credentials to be set."
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """BM25-only index (no model download) and no real GitHub credentials."""
    monkeypatch.setenv("ARCH_EMBEDDER", "none")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    real_run = subprocess.run

    def run(args, *a, **kw):
        if args and args[0] == "gh":
            raise FileNotFoundError("gh")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)


# -- scripted LLM ----------------------------------------------------------------------------


def analysis(system: str, prompt: str) -> dict[str, Any]:
    return {
        "question_type": "why",
        "targets": [{"path": "src/pkg/retry.py", "symbol": "backoff"}],
        "queries": [
            {"source": "code", "query": "backoff MAX_BACKOFF delay"},
            {"source": "commits", "query": "cap backoff sleeps"},
            # matches only PR #15 / issue #9: PR #12 and issue #7 must be reached by hops
            {"source": "prs", "query": "thundering herd"},
            {"source": "docs", "query": "retry delays capped"},
        ],
    }


def grader(scores: Callable[[str], int]) -> Callable[[str, str], dict[str, Any]]:
    def respond(system: str, prompt: str) -> dict[str, Any]:
        grades = [
            {"id": i, "score": scores(i), "reason": "scripted"}
            for i in EVIDENCE_IDS.findall(prompt)
        ]
        return {"grades": grades, "follow": []}

    return respond


def supported(system: str, prompt: str) -> dict[str, Any]:
    return {
        "claims": [{"claim": "the cap avoids long sleeps", "cited_ids": [], "supported": True}],
        "follow_up_queries": [],
    }


def agent_llm(c2: str, prompts: dict[str, list[str]]) -> FakeLLM:
    """Keeps c2, its PR and issue and the backoff code; drafts an answer citing them."""
    keep = {"pr:12": 3, "issue:7": 3, f"commit:{c2}": 3, BACKOFF_ID: 2}

    def draft(system: str, prompt: str) -> dict[str, Any]:
        prompts.setdefault("draft", []).append(prompt)
        return {
            "answer": (
                "Backoff is capped so a retry never sleeps for minutes: users saw workers look "
                f"hung after repeated failures [issue:7]. The cap was added in [pr:12] "
                f"[commit:{c2[:7]}] and set to 30 seconds to match the gateway timeout [pr:12]."
            ),
            "cited_ids": ["pr:12", "issue:7", f"commit:{c2[:7]}"],
            "confidence": "high",
            "missing": [],
        }

    return FakeLLM(
        {
            "Analysis": analysis,
            "Grades": grader(lambda i: keep.get(i, 1)),
            "Draft": draft,
            "Verification": supported,
        }
    )


# -- agent over the real toolbox -------------------------------------------------------------


@pytest.fixture
def toolbox(toy_repo_shas, tmp_path):
    path, shas = toy_repo_shas
    repo = GitRepo(path)
    index = HybridIndex(tmp_path / "index.sqlite", HashEmbedder())
    build_index(repo, index)
    fake = ToyGitHub(shas)
    tb = RepoToolbox(repo, index, fake.client())
    yield tb, fake, shas
    tb.close()


def test_agent_follows_commit_to_pr_and_issue(toolbox):
    tb, fake, shas = toolbox
    c2 = shas[1]
    prompts: dict[str, list[str]] = {}
    llm = agent_llm(c2, prompts)

    answer = ask(QUESTION, tb, llm, GraphConfig())

    assert answer.citations == ["issue:7", "pr:12", f"commit:{c2}"]
    assert f"[commit:{c2}]" in answer.answer  # short sha rewritten to the evidence id
    assert answer.hops >= 1
    by_id = {e.id: e for e in answer.evidence}
    assert {"pr:12", "issue:7", f"commit:{c2}", BACKOFF_ID} <= set(by_id)
    # PR #12 and issue #7 were not found by search: they came from following c2's refs
    assert by_id["pr:12"].source == "fetch" and by_id["issue:7"].source == "fetch"
    assert any(
        line.startswith("follow: hop 1 fetched") and "pr:12" in line for line in answer.trace
    )
    assert any(line.startswith("trace_history:") and "commit" in line for line in answer.trace)
    # real GitHub content (PR discussion, issue body) reached the synthesizer
    draft_prompt = prompts["draft"][-1]
    assert "gateway timeout" in draft_prompt
    assert "workers look hung" in draft_prompt
    assert fake.hits("/repos/acme/toy/pulls/12") == 1
    assert llm.count("Analysis") == 1 and llm.count("Draft") == 1
    assert llm.count("Verification") == 1
    assert answer.unsupported_claims == []


def test_agent_without_hops_cannot_reach_the_pr(toolbox):
    tb, _fake, shas = toolbox
    answer = ask(QUESTION, tb, agent_llm(shas[1], {}), GraphConfig(multi_hop=False))
    assert answer.hops == 0
    assert "pr:12" not in {e.id for e in answer.evidence}
    assert "pr:12" not in answer.citations  # cited but never retrieved: dropped
    assert f"commit:{shas[1]}" in answer.citations


# -- CLI -------------------------------------------------------------------------------------


def cli_llm(c2: str, **overrides: Callable[[str, str], Any]) -> FakeLLM:
    """Offline agent run: keeps commits and code, cites commit c2."""

    def draft(system: str, prompt: str) -> dict[str, Any]:
        assert f"commit:{c2}" in EVIDENCE_IDS.findall(prompt)
        return {
            "answer": f"The cap avoids multi-minute sleeps [commit:{c2}].",
            "cited_ids": [f"commit:{c2}"],
            "confidence": "medium",
            "missing": [],
        }

    def baseline(system: str, prompt: str) -> dict[str, Any]:
        shown = RENDERED_IDS.findall(prompt)
        cite = f"commit:{c2}" if f"commit:{c2}" in shown else shown[0]
        return {"answer": f"Backoff is capped to avoid long sleeps [{cite}]."}

    responders: dict[str, Callable[[str, str], Any]] = {
        "Analysis": analysis,
        "Grades": grader(lambda i: 3 if i.startswith("commit:") else 2),
        "Draft": draft,
        "Verification": supported,
        "BaselineAnswer": baseline,
        "Judgement": lambda s, p: {"reason": "Matches the reference.", "score": 2},
    }
    responders.update(overrides)
    return FakeLLM(responders)


def use_llm(monkeypatch, llm: FakeLLM) -> FakeLLM:
    monkeypatch.setattr(cli, "make_llm", lambda: llm)
    return llm


def test_cli_index(toy_repo):
    result = runner.invoke(cli.app, ["index", str(toy_repo)])
    assert result.exit_code == 0, result.output
    assert "commit" in result.stdout and "4 commits" in result.stdout
    again = runner.invoke(cli.app, ["index", str(toy_repo)])
    assert again.exit_code == 0, again.output
    assert "Up to date" in again.stdout


def test_cli_index_commit_cap_is_kept_by_later_builds(toy_repo):
    result = runner.invoke(cli.app, ["index", str(toy_repo), "--max-commits", "2"])
    assert result.exit_code == 0, result.output
    with open_toolbox(toy_repo, offline=True, embedder=None) as tb:
        assert tb.build_stats["skipped"]  # same cap: nothing to rebuild
        assert tb.index.count("commit") == 2


def test_cli_ask_json_offline(toy_repo_shas, monkeypatch):
    path, shas = toy_repo_shas
    llm = use_llm(monkeypatch, cli_llm(shas[1]))
    result = runner.invoke(cli.app, ["ask", str(path), QUESTION, "--json", "--offline"])
    assert result.exit_code == 0, result.output
    answer = Answer.model_validate_json(result.stdout)  # stdout is pure JSON
    assert answer.citations == [f"commit:{shas[1]}"]
    # offline: the hop to c2's PR and issue finds nothing
    assert all(e.kind not in ("pr", "issue") for e in answer.evidence)
    assert any("not found: pr:12" in line for line in answer.trace)
    assert llm.count("Analysis") == 1 and llm.count("Verification") == 1


def test_cli_ask_steps_and_flags(toy_repo_shas, monkeypatch):
    path, shas = toy_repo_shas
    llm = use_llm(monkeypatch, cli_llm(shas[1]))
    args = ["ask", str(path), QUESTION, "--offline", "--steps", "--no-verify", "--no-hops"]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    for node in ("analyze", "retrieve", "trace_history", "grade", "synthesize", "finalize"):
        assert node in result.stderr
    assert "verify" not in result.stderr.replace("--no-verify", "")
    assert llm.count("Verification") == 0
    assert "Sources" in result.stdout and f"commit:{shas[1]}" in result.stdout


@pytest.mark.parametrize(
    "error",
    [
        TypeError(AUTH_MESSAGE),
        anthropic.AuthenticationError(
            "invalid x-api-key",
            response=httpx.Response(401, request=httpx.Request("POST", "https://example.test")),
            body=None,
        ),
    ],
)
def test_cli_ask_auth_failure_is_friendly(toy_repo, monkeypatch, error):
    def fail(system: str, prompt: str) -> Any:
        raise error

    use_llm(monkeypatch, FakeLLM({"Analysis": fail}))
    result = runner.invoke(cli.app, ["ask", str(toy_repo), QUESTION, "--offline"])
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert "Traceback" not in result.output


def test_cli_ask_rejects_non_repo(tmp_path, monkeypatch):
    use_llm(monkeypatch, FakeLLM())
    result = runner.invoke(cli.app, ["ask", str(tmp_path), QUESTION, "--offline"])
    assert result.exit_code == 2
    assert "not a git repository" in result.stderr


def write_bench(path, c2: str) -> None:
    item = BenchmarkItem(
        id="acme-toy-pr12",
        repo="acme/toy",
        question=QUESTION,
        reference_rationale="Unbounded exponential backoff made workers sleep for minutes.",
        target_path="src/pkg/retry.py",
        target_symbol="backoff",
        gold_pr=12,
        gold_commits=[c2],
        gold_issues=[7],
    )
    path.write_text(item.model_dump_json() + "\n")


def test_cli_eval_offline(toy_repo_shas, tmp_path, monkeypatch):
    path, shas = toy_repo_shas
    bench, out = tmp_path / "bench.jsonl", tmp_path / "results" / "smoke"
    write_bench(bench, shas[1])
    use_llm(monkeypatch, cli_llm(shas[1]))
    systems = "agent_no_verify,plain_rag,single_commit"
    args = ["eval", str(path), str(bench), "--systems", systems, "--offline", "--out", str(out)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    summary = json.loads((out / "summary.json").read_text())
    assert list(summary["systems"]) == systems.split(",")
    rows = [json.loads(line) for line in (out / "results.jsonl").read_text().splitlines()]
    assert [r["system"] for r in rows] == systems.split(",")
    assert all(r["error"] is None and r["judge_score"] == 2 for r in rows)
    by_system = {r["system"]: r for r in rows}
    assert by_system["agent_no_verify"]["gold_hit_cited"]  # cites the gold merge commit
    assert (out / "report.md").read_text().startswith("# Repo Archaeologist benchmark")
    assert "plain_rag" in result.stdout


def test_cli_eval_stops_on_auth_failure(toy_repo_shas, tmp_path, monkeypatch):
    path, shas = toy_repo_shas
    bench, out = tmp_path / "bench.jsonl", tmp_path / "res"
    write_bench(bench, shas[1])

    def fail(system: str, prompt: str) -> Any:
        raise TypeError(AUTH_MESSAGE)

    use_llm(monkeypatch, cli_llm(shas[1], BaselineAnswer=fail))
    args = ["eval", str(path), str(bench), "--systems", "plain_rag", "--offline", "--out", str(out)]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 2
    assert "ANTHROPIC_API_KEY" in result.stderr
    assert not (out / "results.jsonl").exists()  # no row of failures was recorded


def test_cli_eval_rejects_unknown_system(toy_repo, tmp_path):
    bench = tmp_path / "bench.jsonl"
    bench.write_text("")
    result = runner.invoke(cli.app, ["eval", str(toy_repo), str(bench), "--systems", "agent,magic"])
    assert result.exit_code == 2
    assert "magic" in result.output


def test_cli_mine_with_fake_github(toy_repo_shas, tmp_path, monkeypatch):
    path, shas = toy_repo_shas
    fake = ToyGitHub(shas)
    monkeypatch.setattr(cli, "open_github", lambda repo, **kw: fake.client())
    question_prompts: list[str] = []

    def question(system: str, prompt: str) -> dict[str, Any]:
        question_prompts.append(prompt)
        return {
            "question": "Why does backoff() in src/pkg/retry.py never wait longer than a fixed "
            "limit, even after many failed attempts?",
            "trivial": False,
        }

    rationale = {
        "rationale": "Unbounded exponential backoff made workers sleep for minutes after "
        "repeated failures, so the delay is capped to match the gateway timeout.",
        "explains_why": True,
    }
    use_llm(
        monkeypatch,
        FakeLLM({"MinedQuestion": question, "MinedRationale": lambda s, p: rationale}),
    )
    out = tmp_path / "bench.jsonl"
    result = runner.invoke(cli.app, ["mine", str(path), "-n", "5", "-o", str(out)])
    assert result.exit_code == 0, result.output
    items = load_benchmark(out)
    assert [i.id for i in items] == ["acme-toy-pr12"]  # PRs #15 and #3 have short bodies
    item = items[0]
    assert (item.gold_pr, item.gold_commits, item.gold_issues) == (12, [shas[1]], [7])
    assert (item.target_path, item.target_symbol) == ("src/pkg/retry.py", "backoff")
    # leakage guard: the question writer saw the diff only
    assert question_prompts and "Cap backoff at 30s" not in question_prompts[0]
    assert "Fixes #7" not in question_prompts[0]
    assert "acme-toy-pr12" in result.stdout


def test_cli_mine_needs_a_github_remote(toy_repo, tmp_path, monkeypatch):
    subprocess.run(["git", "remote", "remove", "origin"], cwd=toy_repo, check=True)
    use_llm(monkeypatch, FakeLLM())
    result = runner.invoke(cli.app, ["mine", str(toy_repo), "-o", str(tmp_path / "b.jsonl")])
    assert result.exit_code == 2
    assert "GitHub remote" in result.stderr
