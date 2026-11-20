"""Web API: sources, jobs (clone + index), runs streamed over SSE, persistence, website
fallback. Offline: BM25-only index, toolboxes forced offline, a scripted FakeLLM."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_e2e import agent_llm
from typer.testing import CliRunner

from archaeologist import cli
from archaeologist.llm import LLMAuthError
from archaeologist.testing import FakeLLM
from archaeologist.toolbox import open_toolbox
from archaeologist.web import app as web_app
from archaeologist.web.app import create_app
from archaeologist.web.sources import SourceError, parse_source, repo_id_for
from archaeologist.web.store import Store

QUESTION = "Why is backoff capped at 30 seconds?"


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


def sse(client: TestClient, url: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    events = []
    with client.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def open_repo(client: TestClient, source: str) -> tuple[str, list[dict[str, Any]]]:
    resp = client.post("/api/repos", json={"source": source})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    return body["repo_id"], sse(client, f"/api/jobs/{body['job']['id']}/events")


# -- sources ---------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "pallets/click",
    "https://github.com/pallets/click",
    "https://github.com/pallets/click.git",
    "github.com/pallets/click/tree/main/src",
    "git@github.com:pallets/click.git",
])
def test_parse_github_sources(text):
    src = parse_source(text, allow_local=False)
    assert (src.kind, src.slug) == ("github", "pallets/click")
    assert src.path.name == "pallets__click"


def test_parse_local_source(toy_repo):
    src = parse_source(str(toy_repo / "src"), allow_local=True)  # any dir inside the clone
    assert src.kind == "local" and src.path == toy_repo.resolve()
    assert repo_id_for(src.path).startswith("toy-")


@pytest.mark.parametrize("text, allow, message", [
    ("", True, "Enter a GitHub repository"),
    ("/definitely/not/here", True, "No such directory"),
    ("./x", False, "only opens GitHub repositories"),
    ("../..", True, None),  # exists but (in tmp) not a git repo -> checked below
    ("owner/..", True, "not a valid"),
])
def test_parse_source_errors(tmp_path, monkeypatch, text, allow, message):
    monkeypatch.chdir(tmp_path)
    if message is None:
        (tmp_path / "plain").mkdir()
        text, message = str(tmp_path / "plain"), "not a git repository"
    with pytest.raises(SourceError, match=message):
        parse_source(text, allow_local=allow)


# -- status ----------------------------------------------------------------------------------


def test_status_without_key_explains_how_to_fix():
    with make_client() as client:
        status = client.get("/api/status").json()
    assert status["llm_ready"] is False and "ANTHROPIC_API_KEY" in status["llm_hint"]
    assert status["github"] == "none" and status["allow_local_paths"] is True


def test_status_with_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    with make_client() as client:
        status = client.get("/api/status").json()
    assert status["provider"] == "gemini" and status["llm_ready"] is True
    assert status["model"] == "gemini-3.5-flash" and status["grader_model"] == "gemini-3.5-flash-lite"


# -- repositories ----------------------------------------------------------------------------


def test_open_local_repo_indexes_and_lists_it(toy_repo):
    with make_client(FakeLLM()) as client:
        repo_id, events = open_repo(client, str(toy_repo))
        assert events[0] == {"type": "status", "status": "running"}
        assert any(e["type"] == "progress" and e["stage"] in ("files", "commits") for e in events)
        done = events[-1]
        assert done["status"] == "done" and done["repo"]["id"] == repo_id
        repo = client.get(f"/api/repos/{repo_id}").json()
        assert repo["counts"]["commit"] == 4 and repo["counts"]["code"] > 0
        assert repo["slug"] == "acme/toy" and repo["web_url"] == "https://github.com/acme/toy"
        assert repo["head"] and repo["indexed_at"] and repo["runs"] == 0
        assert [r["id"] for r in client.get("/api/repos").json()] == [repo_id]
        # re-index works and keeps the repo
        job = client.post(f"/api/repos/{repo_id}/reindex").json()["job"]
        assert sse(client, f"/api/jobs/{job['id']}/events")[-1]["status"] == "done"
        assert client.delete(f"/api/repos/{repo_id}").status_code == 204
        assert client.get("/api/repos").json() == []
        assert client.get(f"/api/repos/{repo_id}").status_code == 404


def _bare_remote(base: Path, toy_repo: Path, slug: str) -> None:
    dest = base / f"{slug}.git"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--bare", "-q", str(toy_repo), str(dest)], check=True)


def test_open_github_repo_clones_full_history(toy_repo, tmp_path, monkeypatch):
    remotes = tmp_path / "remotes"
    _bare_remote(remotes, toy_repo, "acme/toy")
    monkeypatch.setenv("ARCH_CLONE_BASE", remotes.as_uri())
    with make_client(FakeLLM(), allow_local_paths=False) as client:
        repo_id, events = open_repo(client, "https://github.com/acme/toy")
        assert events[-1]["status"] == "done", events
        assert any(e["type"] == "progress" and e["stage"] == "clone" for e in events)
        repo = client.get(f"/api/repos/{repo_id}").json()
        assert repo["slug"] == "acme/toy" and repo["counts"]["commit"] == 4
        assert repo["path"].endswith("clones/acme__toy") and not repo["shallow"]
        # opening it again pulls instead of cloning
        _, again = open_repo(client, "acme/toy")
        assert any(e["type"] == "progress" and e["stage"] == "pull" for e in again)
        assert again[-1]["status"] == "done"


def test_open_repo_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCH_CLONE_BASE", (tmp_path / "nowhere").as_uri())
    with make_client(FakeLLM()) as client:
        resp = client.post("/api/repos", json={"source": "  "})
        assert resp.status_code == 400 and "Enter a GitHub repository" in resp.json()["detail"]
        _, events = open_repo(client, "ghost/missing")
        assert events[-1]["status"] == "error" and "ghost/missing" in events[-1]["error"]
        assert client.get("/api/repos").json() == []  # never listed
        assert client.get("/api/jobs/nope").status_code == 404


# -- runs ------------------------------------------------------------------------------------


def test_run_streams_steps_and_answer(toy_repo_shas):
    path, shas = toy_repo_shas
    with make_client(agent_llm(shas[1], {})) as client:
        repo_id, _ = open_repo(client, str(path))
        resp = client.post("/api/runs", json={"repo_id": repo_id, "question": f"  {QUESTION} "})
        assert resp.status_code == 202
        run = resp.json()
        assert run["question"] == QUESTION and run["status"] in ("queued", "running")
        assert run["options"] == {"follow_links": True, "verify": True, "offline": False}

        events = sse(client, f"/api/runs/{run['id']}/events")
        assert [e["status"] for e in events if e["type"] == "status"] == ["queued", "running", "done"]
        steps = [e for e in events if e["type"] == "step"]
        nodes = [e["node"] for e in steps]
        for node in ("analyze", "retrieve", "trace_history", "grade", "synthesize", "verify",
                     "finalize"):
            assert node in nodes
        analyze = steps[nodes.index("analyze")]
        assert analyze["analysis"]["queries"][0]["source"] == "code"
        retrieve = [e for e in steps if e["node"] == "retrieve"]
        assert {e["source"] for e in retrieve} == {"code", "commits", "prs", "docs"}
        assert all(isinstance(e["at"], float) for e in steps)
        found = [f for e in steps for f in e.get("evidence", [])]
        assert any(f["kind"] == "commit" and f["text"] is None and f["excerpt"] for f in found)
        grade = steps[nodes.index("grade")]
        assert {"id", "score", "reason"} <= set(grade["grades"][0]) and grade["kept"]

        answer = steps[-1]["answer"]
        assert steps[-1]["node"] == "finalize"
        assert answer["confidence"] == "high"
        assert answer["citations"] == [f"commit:{shas[1]}"]
        # offline: the PR and issue were never fetched, so citing them is dropped, not trusted
        assert {"pr:12", "issue:7"} <= set(answer["dropped_citations"])
        assert all(f["text"] for f in answer["evidence"])
        assert answer["retrieved"] >= len(answer["evidence"]) and answer["model"] == "test-model"

        stored = client.get(f"/api/runs/{run['id']}").json()
        assert stored["status"] == "done" and stored["answer"] == answer
        assert stored["confidence"] == "high" and stored["citations"] == len(answer["citations"])
        assert stored["elapsed_s"] is not None and stored["events"] == events
        listed = client.get(f"/api/runs?repo_id={repo_id}").json()
        assert [r["id"] for r in listed] == [run["id"]] and "events" not in listed[0]
        assert client.get(f"/api/repos/{repo_id}").json()["runs"] == 1

        # resume after the 3rd event replays only what came later
        tail = sse(client, f"/api/runs/{run['id']}/events", {"Last-Event-ID": "2"})
        assert tail == events[3:]


def test_run_options_are_passed_to_the_agent(toy_repo_shas):
    path, shas = toy_repo_shas
    with make_client(agent_llm(shas[1], {})) as client:
        repo_id, _ = open_repo(client, str(path))
        run = client.post("/api/runs", json={
            "repo_id": repo_id, "question": QUESTION, "follow_links": False, "verify": False,
            "offline": True,
        }).json()
        nodes = [e["node"] for e in sse(client, f"/api/runs/{run['id']}/events")
                 if e["type"] == "step"]
        assert "verify" not in nodes and "follow" not in nodes and nodes[-1] == "finalize"


def test_run_rejects_bad_requests(toy_repo):
    with make_client() as client:  # no LLM key configured
        repo_id, _ = open_repo(client, str(toy_repo))
        resp = client.post("/api/runs", json={"repo_id": repo_id, "question": QUESTION})
        assert resp.status_code == 503 and "ANTHROPIC_API_KEY" in resp.json()["detail"]
        assert client.post("/api/runs", json={"repo_id": repo_id, "question": " "}).status_code == 400
        assert client.post("/api/runs", json={"repo_id": "nope", "question": "q"}).status_code == 400
        assert client.get("/api/runs/nope").status_code == 404


def test_run_failure_is_reported_with_its_message(toy_repo):
    def bad_key(system: str, prompt: str):
        raise LLMAuthError("Gemini rejected the API key. Put GEMINI_API_KEY=... in .env")

    with make_client(FakeLLM({"Analysis": bad_key})) as client:
        repo_id, _ = open_repo(client, str(toy_repo))
        run = client.post("/api/runs", json={"repo_id": repo_id, "question": QUESTION}).json()
        events = sse(client, f"/api/runs/{run['id']}/events")
        assert events[-1]["status"] == "error" and "GEMINI_API_KEY" in events[-1]["error"]
        stored = client.get(f"/api/runs/{run['id']}").json()
        assert stored["status"] == "error" and stored["error"] == events[-1]["error"]


def test_backend_warnings_reach_the_run(toy_repo_shas):
    path, shas = toy_repo_shas
    llm = agent_llm(shas[1], {})
    original = llm.responders["Analysis"]

    def noisy(system: str, prompt: str):
        logging.getLogger("archaeologist.gemini").warning("Gemini: HTTP 429; retrying in 7s")
        return original(system, prompt)

    llm.responders["Analysis"] = noisy
    with make_client(llm) as client:
        repo_id, _ = open_repo(client, str(path))
        run = client.post("/api/runs", json={"repo_id": repo_id, "question": QUESTION}).json()
        logs = [e for e in sse(client, f"/api/runs/{run['id']}/events") if e["type"] == "log"]
        assert logs and logs[0]["level"] == "warning" and "429" in logs[0]["message"]
        assert isinstance(logs[0]["at"], float)


def test_interrupted_runs_are_marked_failed(tmp_path):
    store = Store(tmp_path / "w.sqlite")
    store.save_run({"id": "r1", "repo_id": "x", "question": "q", "status": "running",
                    "options": {}, "model": "m", "created_at": "2026-01-01T00:00:00+00:00"})
    store.close()
    run = Store(tmp_path / "w.sqlite").get_run("r1")
    assert run["status"] == "error" and "server stopped" in run["error"]


# -- website ---------------------------------------------------------------------------------


def test_website_fallback_and_assets(tmp_path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>app</title>")
    (static / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("nope")
    with make_client(FakeLLM(), static_dir=static) as client:
        for path in ("/", "/repo/abc", "/run/xyz"):
            resp = client.get(path)
            assert resp.status_code == 200 and "<title>app</title>" in resp.text
        assert client.get("/assets/app.js").text == "console.log(1)"
        assert "immutable" in client.get("/assets/app.js").headers["cache-control"]
        assert "nope" not in client.get("/..%2Fsecret.txt").text
        resp = client.get("/api/does-not-exist")
        assert resp.status_code == 404 and "No API endpoint" in resp.json()["detail"]


def test_website_not_built_page(tmp_path):
    with make_client(FakeLLM(), static_dir=tmp_path / "empty") as client:
        resp = client.get("/")
    assert resp.status_code == 503 and "npm run build" in resp.text


def test_cli_has_serve_command():
    result = CliRunner().invoke(cli.app, ["serve", "--help"])
    assert result.exit_code == 0 and "--port" in result.output


# -- public hosting: access code, CORS, rate limit, size limit, health -------------------------


def test_access_code_guards_the_api(toy_repo):
    with make_client(FakeLLM(), access_code="s3cret") as client:
        status = client.get("/api/status").json()
        assert status["access_required"] is True
        assert client.get("/api/health").json() == {"ok": True}
        resp = client.get("/api/repos")
        assert resp.status_code == 401 and "private" in resp.json()["detail"]
        resp = client.get("/api/repos", headers={"X-Access-Code": "wrong"})
        assert resp.status_code == 401 and "not right" in resp.json()["detail"]
        ok = {"X-Access-Code": "s3cret"}
        assert client.get("/api/repos", headers=ok).json() == []
        job = client.post("/api/repos", json={"source": str(toy_repo)}, headers=ok).json()["job"]
        url = f"/api/jobs/{job['id']}/events"
        assert client.get(url).status_code == 401  # streams need the code too...
        assert sse(client, f"{url}?access=s3cret")[-1]["status"] == "done"  # ...as a query param


def test_no_access_code_by_default():
    with make_client(FakeLLM()) as client:
        assert client.get("/api/status").json()["access_required"] is False
        assert client.get("/api/repos").status_code == 200


def test_cors_is_off_by_default_and_on_when_configured():
    origin = {"Origin": "https://archaeologist.vercel.app"}
    with make_client(FakeLLM()) as client:
        assert "access-control-allow-origin" not in client.get("/api/status", headers=origin).headers
    with make_client(FakeLLM(), cors_origins=["*"], access_code="c") as client:
        pre = client.options("/api/runs", headers={
            **origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-access-code",
        })
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "*"
        denied = client.get("/api/repos", headers=origin)  # the browser must be able to read 401s
        assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == "*"


def test_questions_are_rate_limited_per_address(toy_repo_shas):
    path, shas = toy_repo_shas
    with make_client(agent_llm(shas[1], {}), runs_per_hour=1) as client:
        repo_id, _ = open_repo(client, str(path))
        body = {"repo_id": repo_id, "question": QUESTION, "offline": True}
        first = client.post("/api/runs", json=body, headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9"})
        assert first.status_code == 202
        sse(client, f"/api/runs/{first.json()['id']}/events")  # let it finish
        # same proxy-added address (last entry), forged first entry: still limited
        again = client.post("/api/runs", json=body, headers={"X-Forwarded-For": "2.2.2.2, 9.9.9.9"})
        assert again.status_code == 429 and "60 minutes" in again.json()["detail"]
        other = client.post("/api/runs", json=body, headers={"X-Forwarded-For": "8.8.8.8"})
        assert other.status_code == 202
        sse(client, f"/api/runs/{other.json()['id']}/events")


def test_large_github_repos_are_refused(toy_repo, tmp_path, monkeypatch):
    from archaeologist.web import sources

    remotes = tmp_path / "remotes"
    _bare_remote(remotes, toy_repo, "big/repo")
    monkeypatch.setenv("ARCH_CLONE_BASE", remotes.as_uri())
    monkeypatch.setenv("ARCH_MAX_REPO_MB", "100")
    monkeypatch.setattr(sources, "github_repo_size_mb", lambda slug: 4321.0)
    with make_client(FakeLLM()) as client:
        _, events = open_repo(client, "big/repo")
        assert events[-1]["status"] == "error" and "too large" in events[-1]["error"]
        assert "4,321 MB" in events[-1]["error"] and "100 MB" in events[-1]["error"]
    monkeypatch.setattr(sources, "github_repo_size_mb", lambda slug: 3.0)
    with make_client(FakeLLM()) as client:
        assert open_repo(client, "big/repo")[1][-1]["status"] == "done"
