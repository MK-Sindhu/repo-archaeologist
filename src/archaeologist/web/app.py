"""FastAPI app: the JSON + SSE API (contract: frontend/src/api/types.ts) and the built frontend.

    uvicorn "archaeologist.web.app:create_app" --factory      # or: arch serve
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..backends import backend_config, default_llm, llm_ready
from ..config import arch_home
from ..contracts import LLM
from ..sources.github import resolve_token
from .engine import Engine
from .sources import SourceError, parse_source
from .store import Store

STATIC_DIR = Path(__file__).parent / "static"
NOT_BUILT = """<!doctype html><meta charset="utf-8"><title>Archaeologist</title>
<body style="font:16px system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>The website is not built yet</h1>
<p>Build it once, then reload this page:</p>
<pre>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>Or run <code>./start.sh</code>, which does this for you. The API is up at
<a href="/api/status">/api/status</a>.</p></body>"""


class AddRepoBody(BaseModel):
    source: str


class CreateRunBody(BaseModel):
    repo_id: str
    question: str
    follow_links: bool = True
    verify: bool = True
    offline: bool = False


def _version() -> str:
    try:
        return version("repo-archaeologist")
    except PackageNotFoundError:  # pragma: no cover
        return "dev"


_GH_CACHE: dict[str, Any] = {"at": -1e9, "value": "none"}


def _github_state() -> str:
    """"token" / "gh" / "none". The `gh auth token` probe is cached for a minute."""
    if os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"):
        return "token"
    now = time.monotonic()
    if now - _GH_CACHE["at"] > 60:
        try:
            _GH_CACHE["value"] = "gh" if resolve_token(None) else "none"
        except Exception:  # noqa: BLE001
            _GH_CACHE["value"] = "none"
        _GH_CACHE["at"] = now
    return _GH_CACHE["value"]


def create_app(
    *,
    llm_factory: Callable[[], LLM] | None = None,
    allow_local_paths: bool | None = None,
    static_dir: Path | None = None,
    db_path: Path | None = None,
    max_runs: int | None = None,
    toolbox_opener: Callable[..., Any] | None = None,
) -> FastAPI:
    """Build the app. `llm_factory` overrides the configured backend (tests pass a FakeLLM
    factory); local paths are allowed unless ARCH_WEB_ALLOW_LOCAL=0."""
    if allow_local_paths is None:
        allow_local_paths = os.getenv("ARCH_WEB_ALLOW_LOCAL", "1") not in ("0", "false")
    static = static_dir or STATIC_DIR
    injected = llm_factory is not None
    store = Store(db_path or arch_home() / "web.sqlite")

    def model_name() -> str:
        if injected:
            cfg = getattr(llm_factory(), "config", None)  # type: ignore[misc]
            return getattr(cfg, "model", "test-model")
        return backend_config()[1].model

    engine_kwargs: dict[str, Any] = {}
    if toolbox_opener is not None:
        engine_kwargs["toolbox_opener"] = toolbox_opener
    engine = Engine(
        store,
        llm_factory or default_llm,
        model_name,
        max_runs=max_runs or int(os.getenv("ARCH_WEB_MAX_RUNS", "1") or 1),
        **engine_kwargs,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.close()
        store.close()

    app = FastAPI(title="Repo Archaeologist", version=_version(), lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.engine = engine
    app.state.store = store

    def ready() -> tuple[bool, str | None]:
        if injected:
            return True, None
        try:
            return llm_ready()
        except ValueError as exc:
            return False, str(exc)

    # -- status -------------------------------------------------------------------------
    @app.get("/api/status")
    def status() -> dict[str, Any]:
        ok, hint = ready()
        try:
            provider, cfg = backend_config()
            model, grader = cfg.model, cfg.grader_model
        except ValueError:
            provider, model, grader = "anthropic", "?", "?"
        if injected:
            model = grader = model_name()
        return {
            "provider": provider,
            "model": model,
            "grader_model": grader,
            "llm_ready": ok,
            "llm_hint": hint,
            "github": _github_state(),
            "allow_local_paths": allow_local_paths,
            "version": _version(),
        }

    # -- repos --------------------------------------------------------------------------
    @app.get("/api/repos")
    def list_repos() -> list[dict[str, Any]]:
        return [r for r in store.list_repos() if r["indexed_at"] or _job_pending(engine, r["id"])]

    @app.get("/api/repos/{repo_id}")
    def get_repo(repo_id: str) -> dict[str, Any]:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(404, "This repository is not open. Open it from the home page.")
        return repo

    @app.post("/api/repos", status_code=202)
    def add_repo(body: AddRepoBody) -> dict[str, Any]:
        try:
            source = parse_source(body.source, allow_local=allow_local_paths)
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from None
        job = engine.add_repo(source)
        return {"repo_id": job.repo_id, "job": job.ref()}

    @app.post("/api/repos/{repo_id}/reindex", status_code=202)
    def reindex(repo_id: str) -> dict[str, Any]:
        job = engine.reindex(repo_id)
        if job is None:
            raise HTTPException(404, "This repository is not open. Open it from the home page.")
        return {"job": job.ref()}

    @app.delete("/api/repos/{repo_id}", status_code=204)
    def forget_repo(repo_id: str) -> Response:
        if not engine.forget_repo(repo_id):
            raise HTTPException(404, "This repository is not open.")
        return Response(status_code=204)

    # -- jobs ---------------------------------------------------------------------------
    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        job = engine.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job (jobs are forgotten when the server restarts).")
        return job.view()

    @app.get("/api/jobs/{job_id}/events")
    def job_events(job_id: str, request: Request) -> StreamingResponse:
        job = engine.jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job (jobs are forgotten when the server restarts).")
        return _sse(job.channel.sse(_resume_from(request)))

    # -- runs ---------------------------------------------------------------------------
    @app.post("/api/runs", status_code=202)
    def create_run(body: CreateRunBody) -> dict[str, Any]:
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "Type a question first.")
        if store.get_repo(body.repo_id) is None:
            raise HTTPException(400, "This repository is not open. Open it from the home page.")
        ok, hint = ready()
        if not ok:
            raise HTTPException(503, hint)
        options = {"follow_links": body.follow_links, "verify": body.verify,
                   "offline": body.offline}
        run = engine.start_run(body.repo_id, question, options)
        return dict(run.record)

    @app.get("/api/runs")
    def list_runs(repo_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        stored = {r["id"]: r for r in store.list_runs(repo_id, max(1, min(limit, 200)))}
        for live in list(engine.runs.values()):  # live records are newer than the stored copy
            if live.record["id"] in stored:
                stored[live.record["id"]] = dict(live.record)
        return list(stored.values())

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = engine.get_run(run_id)
        if run is None:
            raise HTTPException(404, "This question was not found. It may have been removed.")
        return run

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, request: Request) -> StreamingResponse:
        channel = engine.run_channel(run_id)
        if channel is None:
            raise HTTPException(404, "This question was not found. It may have been removed.")
        return _sse(channel.sse(_resume_from(request)))

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse({"detail": f"No API endpoint /api/{rest}"}, status_code=404)

    # -- website ------------------------------------------------------------------------
    @app.get("/{path:path}", include_in_schema=False)
    def website(path: str) -> Response:
        index = static / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, status_code=503)
        target = (static / path).resolve()
        if path and target.is_file() and target.is_relative_to(static.resolve()):
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if (
                path.startswith("assets/")) else {}
            return FileResponse(target, headers=headers)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def _job_pending(engine: Engine, repo_id: str) -> bool:
    return any(j.repo_id == repo_id and j.status in ("queued", "running")
               for j in engine.jobs.values())


def _resume_from(request: Request) -> int:
    raw = request.headers.get("last-event-id", "")
    return int(raw) + 1 if raw.isdigit() else 0


def _sse(stream) -> StreamingResponse:
    return StreamingResponse(stream, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
    })
