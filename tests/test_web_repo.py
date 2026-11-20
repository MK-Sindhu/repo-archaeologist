"""Repository endpoints behind the Map, Ask and Guide tabs: survey, outline, code, history,
suggestions, field guide, artifacts, and the website's page routing. Offline: BM25-only
index, toolboxes forced offline, scripted FakeLLM."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_e2e import agent_llm

from archaeologist.sources.git import GitRepo
from archaeologist.testing import FakeLLM
from archaeologist.toolbox import open_toolbox
from archaeologist.web import app as web_app
from archaeologist.web.app import create_app
from archaeologist.web.engine import JobState
from archaeologist.web.store import Store
from archaeologist.web.survey import compute_survey

QUESTION = "Why is backoff capped at 30 seconds?"
EVIDENCE_IDS = re.compile(r'<evidence id="([^"]+)">')


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv("ARCH_EMBEDDER", "none")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(web_app, "resolve_token", lambda _=None: None)


def offline_opener(path, offline=False, **kw):
    return open_toolbox(path, offline=True, **kw)


def make_client(llm=None, **kw) -> TestClient:
    factory = (lambda: llm) if llm is not None else None
    return TestClient(create_app(llm_factory=factory, toolbox_opener=offline_opener, **kw))


def sse(client: TestClient, url: str) -> list[dict[str, Any]]:
    events = []
    with client.stream("GET", url) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def open_repo(client: TestClient, source: str) -> tuple[str, list[dict[str, Any]]]:
    resp = client.post("/api/repos", json={"source": source})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    events = sse(client, f"/api/jobs/{body['job']['id']}/events")
    assert events[-1]["status"] == "done", events
    return body["repo_id"], events


def idle(client: TestClient) -> None:
    client.app.state.engine.join_background(timeout=20)


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp())


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   cwd=cwd, check=True, capture_output=True)


def single_commit_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text('"""Entry."""\n\n\ndef run():\n    return 1\n')
    (root / "README.md").write_text("# fresh\n\nA new project.\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "Initial commit")
    return root


# -- survey ----------------------------------------------------------------------------------


def test_survey_has_exact_numbers_for_the_toy_repo(toy_repo_shas):
    path, shas = toy_repo_shas
    s = compute_survey(GitRepo(path), now=epoch("2024-06-01T00:00:00"))
    assert s["status"] == "ready" and s["head"] == shas[3] and s["truncated"] is False
    assert s["totals"]["commits"] == 4 and s["totals"]["authors"] == 1
    assert s["totals"]["files"] == 2
    assert s["totals"]["first_commit"] == "2024-01-15T12:00:00+00:00"
    assert s["totals"]["last_commit"] == "2024-04-15T12:00:00+00:00"
    files = {f[0]: f for f in s["files"]}
    assert files["src/pkg/retry.py"][2:] == [3, epoch("2024-03-15T12:00:00")]
    assert files["README.md"][2:] == [2, epoch("2024-04-15T12:00:00")]
    assert s["months"] == [{"month": f"2024-0{m}", "commits": 1} for m in (1, 2, 3, 4)]
    assert s["authors"] == [{"name": "Dev", "commits": 4}]
    assert [h["path"] for h in s["hotspots"]] == ["src/pkg/retry.py", "README.md"]
    assert s["hotspots"][0]["changes"] == 3 and s["hotspots"][0]["recent_changes"] == 3
    assert [g["name"] for g in s["languages"]] == ["Python", "Markdown"]


def test_survey_follows_renames_and_marks_truncation(toy_repo):
    git(toy_repo, "mv", "README.md", "DOCS.md")
    git(toy_repo, "commit", "-q", "-m", "Rename the readme")
    s = compute_survey(GitRepo(toy_repo))
    files = {f[0]: f for f in s["files"]}
    assert "README.md" not in files and files["DOCS.md"][2] == 3  # added, edited, renamed
    capped = compute_survey(GitRepo(toy_repo), max_count=2)
    assert capped["truncated"] is True and capped["totals"]["commits"] == 5


# -- artifacts -------------------------------------------------------------------------------


def test_artifacts_are_valid_only_for_their_head_and_version(tmp_path):
    store = Store(tmp_path / "w.sqlite")
    store.upsert_repo({"id": "r", "name": "r", "slug": None, "path": "/x", "web_url": None,
                       "added_at": "t", "indexed_at": "t", "head": None, "counts": None,
                       "shallow": False, "last_used_at": "t"})
    store.put_artifact("r", "survey", head="a" * 40, version=1, data={"n": 1})
    assert store.valid_artifact("r", "survey", head="a" * 40, version=1) == {"n": 1}
    assert store.valid_artifact("r", "survey", head="b" * 40, version=1) is None
    assert store.valid_artifact("r", "survey", head="a" * 40, version=2) is None
    assert store.valid_artifact("r", "survey", head=None, version=1) is None
    assert "survey" not in json.dumps(store.list_repos())  # repos rows don't carry artifacts
    store.delete_repo("r")
    assert store.get_artifact("r", "survey") is None


# -- open job, survey endpoint, pending -----------------------------------------------------


def test_open_job_surveys_and_suggests_before_done(toy_repo):
    with make_client() as client:  # no LLM key configured
        repo_id, events = open_repo(client, str(toy_repo))
        stages = [e["stage"] for e in events if e["type"] == "progress"]
        assert stages.index("survey") < stages.index("suggest")
        survey = client.get(f"/api/repos/{repo_id}/survey").json()
        assert survey["status"] == "ready" and survey["totals"]["commits"] == 4
        sugg = client.get(f"/api/repos/{repo_id}/suggestions").json()
        assert sugg["status"] == "ready" and sugg["source"] == "template"
        questions = [i["question"] for i in sugg["items"]]
        assert questions[0] == "What does this repository do, and how is it organised?"
        assert "How is `pkg` used?" in questions
        why = [i for i in sugg["items"] if i["kind"] == "why"]
        assert why and why[0]["symbol"] == "backoff" and why[0]["path"] == "src/pkg/retry.py"
        resp = client.post(f"/api/repos/{repo_id}/suggestions")
        assert resp.status_code == 503 and "API_KEY" in resp.json()["detail"]
        assert client.get("/api/repos/nope/survey").status_code == 404


def test_endpoints_answer_pending_while_a_job_runs(toy_repo):
    with make_client(FakeLLM()) as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        idle(client)
        engine = client.app.state.engine
        fake = JobState(id="busy", kind="reindex", repo_id=repo_id, status="running")
        engine.jobs["busy"] = fake
        for url in ("survey", "outline?path=src/pkg/retry.py",
                    "history?path=src/pkg/retry.py&symbol=backoff"):
            assert client.get(f"/api/repos/{repo_id}/{url}").json() == {"status": "pending"}
        assert client.get(f"/api/repos/{repo_id}/suggestions").json()["status"] == "pending"
        fake.status = "done"
        assert client.get(f"/api/repos/{repo_id}/survey").json()["status"] == "ready"


def test_survey_is_computed_on_demand_when_missing(toy_repo):
    with make_client() as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        client.app.state.store.delete_artifact(repo_id, "survey")
        assert client.get(f"/api/repos/{repo_id}/survey").json()["totals"]["commits"] == 4
        assert client.app.state.store.get_artifact(repo_id, "survey") is not None


# -- suggestions with an LLM -----------------------------------------------------------------


def suggester(prompt_box: list[str]):
    def respond(system: str, prompt: str) -> dict[str, Any]:
        prompt_box.append(prompt)
        return {"items": [
            {"question": "How does Retrier.run decide when to give up?", "kind": "how",
             "path": "src/pkg/retry.py", "symbol": "Retrier.run"},
            {"question": "Where are retry delays computed?", "kind": "where"},
            {"question": "What does this toy package do?", "kind": "what"},
            {"question": "Why does backoff() cap the delay at MAX_BACKOFF?", "kind": "why",
             "path": "src/pkg/retry.py", "symbol": "backoff"},
            {"question": "Why was #12 needed for backoff?", "kind": "why", "symbol": "backoff"},
            {"question": "Why does frobnicate() exist?", "kind": "why", "symbol": "frobnicate"},
        ]}
    return respond


def test_llm_writes_suggestions_in_the_background(toy_repo):
    prompts: list[str] = []
    llm = FakeLLM({"SuggestionList": suggester(prompts)})
    with make_client(llm) as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        idle(client)
        sugg = client.get(f"/api/repos/{repo_id}/suggestions").json()
        assert sugg["status"] == "ready" and sugg["source"] == "llm"
        qs = [i["question"] for i in sugg["items"]]
        assert qs[:3] == ["How does Retrier.run decide when to give up?",
                          "Where are retry delays computed?", "What does this toy package do?"]
        why = [i for i in sugg["items"] if i["kind"] == "why"]
        # grounded in a candidate, no PR numbers, unknown symbols dropped
        assert why == [{"question": "Why does backoff() cap the delay at MAX_BACKOFF?",
                        "kind": "why", "path": "src/pkg/retry.py", "symbol": "backoff"}]
        assert "symbol: backoff" in prompts[0] and "<candidates>" in prompts[0]
        assert llm.count("SuggestionList") == 1
        # regenerating calls the LLM once more; GET alone does not
        client.get(f"/api/repos/{repo_id}/suggestions")
        idle(client)
        assert llm.count("SuggestionList") == 1
        assert client.post(f"/api/repos/{repo_id}/suggestions").json()["status"] == "pending"
        idle(client)
        assert llm.count("SuggestionList") == 2


def test_failed_llm_keeps_templates(toy_repo):
    with make_client(FakeLLM()) as client:  # no SuggestionList responder: the call fails
        repo_id, _ = open_repo(client, str(toy_repo))
        idle(client)
        sugg = client.get(f"/api/repos/{repo_id}/suggestions").json()
        assert sugg["status"] == "failed" and sugg["source"] == "template" and sugg["items"]


def test_thin_history_gets_orientation_questions_only(tmp_path):
    repo = single_commit_repo(tmp_path / "fresh")
    llm = FakeLLM({"SuggestionList": suggester([])})
    with make_client(llm) as client:
        repo_id, _ = open_repo(client, str(repo))
        idle(client)
        sugg = client.get(f"/api/repos/{repo_id}/suggestions").json()
        assert sugg["items"] and all(i["kind"] != "why" for i in sugg["items"])
        survey = client.get(f"/api/repos/{repo_id}/survey").json()
        assert survey["totals"]["commits"] == 1 and survey["months"] == [
            {"month": survey["months"][0]["month"], "commits": 1}]


# -- outline / code ------------------------------------------------------------------------


def test_outline_and_code(toy_repo):
    with make_client(FakeLLM()) as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        out = client.get(f"/api/repos/{repo_id}/outline", params={"path": "src/pkg/retry.py"}).json()
        assert out["status"] == "ready" and out["supported"] and out["language"] == "python"
        syms = {s["qualified"]: s for s in out["symbols"]}
        assert syms["backoff"]["kind"] == "function" and (syms["backoff"]["start"], syms["backoff"]["end"]) == (8, 13)
        assert syms["backoff"]["doc"] == "Seconds to wait before retry number `attempt`."
        assert syms["Retrier.run"]["kind"] == "method"
        doc = client.get(f"/api/repos/{repo_id}/outline", params={"path": "README.md"}).json()
        assert doc["supported"] is False and doc["symbols"] == []

        code = client.get(f"/api/repos/{repo_id}/code",
                          params={"path": "src/pkg/retry.py", "start": 8, "end": 13}).json()
        assert code["text"].startswith("def backoff(attempt, jitter=True):")
        assert (code["start"], code["end"], code["total_lines"]) == (8, 13, 26)
        clamp = client.get(f"/api/repos/{repo_id}/code",
                           params={"path": "src/pkg/retry.py", "start": 99, "end": 5000}).json()
        assert (clamp["start"], clamp["end"]) == (26, 26)
        whole = client.get(f"/api/repos/{repo_id}/code", params={"path": "src/pkg/retry.py"}).json()
        assert (whole["start"], whole["end"]) == (1, 26)
        for bad in ("../../etc/passwd", "/etc/passwd", "src/pkg/nope.py", "src/pkg"):
            assert client.get(f"/api/repos/{repo_id}/code", params={"path": bad}).status_code == 404
            assert client.get(f"/api/repos/{repo_id}/outline", params={"path": bad}).status_code == 404


def test_code_is_capped_at_400_lines(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "big.py").write_text("".join(f"x{i} = {i}\n" for i in range(1000)))
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "big")
    with make_client(FakeLLM()) as client:
        repo_id, _ = open_repo(client, str(root))
        code = client.get(f"/api/repos/{repo_id}/code", params={"path": "big.py", "start": 10}).json()
        assert (code["start"], code["end"], code["total_lines"]) == (10, 409, 1000)


# -- history ---------------------------------------------------------------------------------


def test_history_uses_commit_messages_only_unless_resolved(toy_repo_shas, monkeypatch):
    path, shas = toy_repo_shas
    with make_client(FakeLLM()) as client:
        repo_id, _ = open_repo(client, str(path))
        engine = client.app.state.engine
        monkeypatch.setattr(engine, "read_toolbox",
                            lambda rid: pytest.fail("history must not touch GitHub"))
        h = client.get(f"/api/repos/{repo_id}/history", params={
            "path": "src/pkg/retry.py", "symbol": "backoff", "start": 8, "end": 13}).json()
        assert h["status"] == "ready" and h["resolved"] is False and h["prs"] == []
        assert [c["id"] for c in h["commits"]] == [f"commit:{s}" for s in (shas[2], shas[1], shas[0])]
        assert "pr:12" in h["commits"][1]["refs"] and h["commits"][0]["source"] == "history"
        whole = client.get(f"/api/repos/{repo_id}/history", params={"path": "README.md"}).json()
        assert len(whole["commits"]) == 2

        from archaeologist.models import Evidence

        calls: list[str] = []

        class Github:
            def prs_for_commit(self, sha):
                calls.append(sha)
                return []

        class Toolbox:
            github = Github()

            def fetch(self, ref):
                return Evidence(id=ref, kind="pr" if ref.startswith("pr") else "issue",
                                title=f"fetched {ref}", text="body")

        monkeypatch.setattr(engine, "read_toolbox", lambda rid: Toolbox())
        r = client.get(f"/api/repos/{repo_id}/history", params={
            "path": "src/pkg/retry.py", "symbol": "backoff", "start": 8, "end": 13,
            "resolve": 1}).json()
        assert r["resolved"] is True
        assert {p["id"] for p in r["prs"]} >= {"pr:12", "pr:15"}
        assert calls == [shas[0]]  # only the commit whose message names no PR


# -- field guide ----------------------------------------------------------------------------


def guide_writer(box: list[str]):
    def respond(system: str, prompt: str) -> dict[str, Any]:
        box.append(prompt)
        ids = EVIDENCE_IDS.findall(prompt)
        code = next(i for i in ids if i.startswith("code:"))
        doc = next(i for i in ids if i.startswith("doc:"))
        return {"sections": [
            {"title": "Where to look first", "markdown": f"Start with backoff [{code}]."},
            {"title": "What it is", "markdown": f"A toy retry package [{doc}] [code:invented.py#L1-L2]."},
        ]}
    return respond


def test_guide_is_written_on_request_and_cited(toy_repo):
    prompts: list[str] = []
    llm = FakeLLM({"GuideDraft": guide_writer(prompts), "SuggestionList": suggester([])})
    with make_client(llm, runs_per_hour=3) as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        idle(client)
        assert client.get(f"/api/repos/{repo_id}/guide").json()["status"] == "missing"
        assert llm.count("GuideDraft") == 0  # never automatic
        resp = client.post(f"/api/repos/{repo_id}/guide")
        assert resp.status_code == 202 and resp.json()["job"]["kind"] == "guide"
        events = sse(client, f"/api/jobs/{resp.json()['job']['id']}/events")
        assert [e["stage"] for e in events if e["type"] == "progress"][:3] == ["gather", "write", "check"]
        assert events[-1]["status"] == "done"
        guide = client.get(f"/api/repos/{repo_id}/guide").json()
        assert guide["status"] == "ready" and guide["job"] is None
        assert guide["markdown"].startswith("## What it is")  # sections in the fixed order
        assert "## Where to look first" in guide["markdown"]
        assert "invented" not in guide["markdown"] and len(guide["citations"]) == 2
        assert all(f["text"] for f in guide["evidence"])
        assert {f["id"] for f in guide["evidence"]} == set(guide["citations"])
        assert "<facts>" in prompts[0] and "Main package or module: pkg" in prompts[0]
        # the guide counted 3 against the hourly limit of 3
        run = client.post("/api/runs", json={"repo_id": repo_id, "question": QUESTION})
        assert run.status_code == 429


def test_guide_needs_a_key_and_reports_failure(toy_repo):
    with make_client() as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        assert client.post(f"/api/repos/{repo_id}/guide").status_code == 503
    with make_client(FakeLLM()) as client:  # no GuideDraft responder
        repo_id, _ = open_repo(client, str(toy_repo))
        job = client.post(f"/api/repos/{repo_id}/guide").json()["job"]
        assert sse(client, f"/api/jobs/{job['id']}/events")[-1]["status"] == "error"
        guide = client.get(f"/api/repos/{repo_id}/guide").json()
        assert guide["status"] == "failed" and guide["error"]


# -- answers carry their mode -------------------------------------------------------------------


def test_answer_payload_has_a_mode(toy_repo_shas):
    path, shas = toy_repo_shas
    with make_client(agent_llm(shas[1], {})) as client:
        repo_id, _ = open_repo(client, str(path))
        run = client.post("/api/runs", json={"repo_id": repo_id, "question": QUESTION}).json()
        events = sse(client, f"/api/runs/{run['id']}/events")
        answer = next(e["answer"] for e in events if e.get("node") == "finalize")
        assert answer["mode"] in ("why", "explain")


# -- website routing ---------------------------------------------------------------------------


def test_website_serves_prerendered_pages_and_the_app_shell(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "how-it-works").mkdir()
    (static / "examples" / "demo").mkdir(parents=True)
    (static / "index.html").write_text("<title>landing</title>")
    (static / "app.html").write_text("<title>app</title>")
    (static / "how-it-works" / "index.html").write_text("<title>how</title>")
    (static / "examples" / "demo" / "index.html").write_text("<title>demo</title>")
    (static / "faq.html").write_text("<title>faq</title>")
    with make_client(FakeLLM(), static_dir=static) as client:
        def title(path: str) -> str:
            resp = client.get(path)
            assert resp.status_code == 200, path
            return re.search(r"<title>(.*)</title>", resp.text).group(1)

        assert title("/") == "landing"
        assert title("/how-it-works") == title("/how-it-works/") == "how"
        assert title("/examples/demo") == "demo"
        assert title("/faq") == "faq"
        for app_path in ("/app", "/app/", "/app/repo/x", "/app/run/y", "/repo/x", "/run/y"):
            assert title(app_path) == "app", app_path
        assert title("/self-host") == "landing"  # not pre-rendered: the site shell handles it
        assert title("/repo") == "landing"
    (static / "404.html").write_text("<title>lost</title>")
    with make_client(FakeLLM(), static_dir=static) as client:
        lost = client.get("/no-such-page")
        assert lost.status_code == 404 and "<title>lost</title>" in lost.text
        assert client.get("/").status_code == 200  # the landing page is never the 404
        assert "<title>app</title>" in client.get("/app/repo/x").text
    (static / "404.html").unlink()
    (static / "app.html").unlink()
    with make_client(FakeLLM(), static_dir=static) as client:
        assert "landing" in client.get("/app/repo/x").text  # no app.html: fall back to index
