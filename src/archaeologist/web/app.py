"""FastAPI app: the JSON + SSE API (contract: frontend/src/api/types.ts) and the built frontend.

    uvicorn "archaeologist.web.app:create_app" --factory      # or: arch serve
"""

from __future__ import annotations

import hmac
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..backends import backend_config, default_llm, llm_ready
from ..config import arch_home
from ..contracts import LLM
from ..sources.github import resolve_token
from .engine import Engine
from .repo_api import NotFound, RepoApi
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
    access_code: str | None = None,
    cors_origins: list[str] | None = None,
    runs_per_hour: int | None = None,
) -> FastAPI:
    """Build the app. `llm_factory` overrides the configured backend (tests pass a FakeLLM
    factory). Other settings default to the environment:

    - ARCH_WEB_ALLOW_LOCAL=0      only GitHub repositories (no local paths); default allowed
    - ARCH_ACCESS_CODE=<code>     every /api endpoint but status/health needs this code
    - ARCH_CORS_ORIGINS=a,b|*     allow the website on other origins (default: same origin)
    - ARCH_RUNS_PER_HOUR=<n>      questions per client address per hour (default: no limit)
    """
    if allow_local_paths is None:
        allow_local_paths = os.getenv("ARCH_WEB_ALLOW_LOCAL", "1") not in ("0", "false")
    if access_code is None:
        access_code = os.getenv("ARCH_ACCESS_CODE", "").strip()
    if cors_origins is None:
        cors_origins = [o.strip() for o in os.getenv("ARCH_CORS_ORIGINS", "").split(",") if o.strip()]
    if runs_per_hour is None:
        runs_per_hour = int(os.getenv("ARCH_RUNS_PER_HOUR", "0") or 0)
    limiter = _RateLimiter(runs_per_hour)
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
    def llm_is_ready() -> bool:
        if injected:
            return True
        try:
            return llm_ready()[0]
        except ValueError:
            return False

    engine = Engine(
        store,
        llm_factory or default_llm,
        model_name,
        max_runs=max_runs or int(os.getenv("ARCH_WEB_MAX_RUNS", "1") or 1),
        llm_ready=llm_is_ready,
        **engine_kwargs,
    )
    repos_api = RepoApi(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.close()
        store.close()

    app = FastAPI(title="Repo Archaeologist", version=_version(), lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.state.engine = engine
    app.state.store = store

    @app.middleware("http")
    async def require_access_code(request: Request, call_next):
        path = request.url.path
        if (access_code and path.startswith("/api/") and path not in OPEN_PATHS
                and request.method != "OPTIONS"):
            given = request.headers.get("x-access-code") or request.query_params.get("access") or ""
            if not hmac.compare_digest(given.encode(), access_code.encode()):
                detail = ("That access code is not right." if given
                          else "This site is private. Enter the access code to continue.")
                return JSONResponse({"detail": detail}, status_code=401)
        return await call_next(request)

    if cors_origins:  # added last so it wraps everything, 401s included
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-Access-Code", "Last-Event-ID"],
            max_age=600,
        )

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
            "access_required": bool(access_code),
            "version": _version(),
        }

    @app.get("/api/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

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

    # -- repository: survey, files, history, suggestions, guide ---------------------------
    def _repo_call(fn, *args):
        try:
            return fn(*args)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/repos/{repo_id}/survey")
    def repo_survey(repo_id: str) -> dict[str, Any]:
        return _repo_call(repos_api.survey, repo_id)

    @app.get("/api/repos/{repo_id}/outline")
    def repo_outline(repo_id: str, path: str) -> dict[str, Any]:
        return _repo_call(repos_api.outline, repo_id, path)

    @app.get("/api/repos/{repo_id}/code")
    def repo_code(repo_id: str, path: str, start: int | None = None,
                  end: int | None = None) -> dict[str, Any]:
        return _repo_call(repos_api.code, repo_id, path, start, end)

    @app.get("/api/repos/{repo_id}/history")
    def repo_history(repo_id: str, path: str, symbol: str | None = None,
                     start: int | None = None, end: int | None = None,
                     resolve: int = 0) -> dict[str, Any]:
        return _repo_call(repos_api.history, repo_id, path, symbol, start, end, bool(resolve))

    @app.get("/api/repos/{repo_id}/suggestions")
    def repo_suggestions(repo_id: str) -> dict[str, Any]:
        return _repo_call(repos_api.suggestions, repo_id)

    @app.post("/api/repos/{repo_id}/suggestions", status_code=202)
    def repo_refresh_suggestions(repo_id: str, request: Request) -> dict[str, Any]:
        if store.get_repo(repo_id) is None:
            raise HTTPException(404, "This repository is not open. Open it from the home page.")
        ok, hint = ready()
        if not ok:
            raise HTTPException(503, hint)
        _limit(limiter, request)
        return _repo_call(repos_api.refresh_suggestions, repo_id)

    @app.get("/api/repos/{repo_id}/guide")
    def repo_guide(repo_id: str) -> dict[str, Any]:
        return _repo_call(repos_api.guide, repo_id)

    @app.post("/api/repos/{repo_id}/guide", status_code=202)
    def repo_write_guide(repo_id: str, request: Request) -> dict[str, Any]:
        if store.get_repo(repo_id) is None:
            raise HTTPException(404, "This repository is not open. Open it from the home page.")
        ok, hint = ready()
        if not ok:
            raise HTTPException(503, hint)
        if engine.guide_job(repo_id) is None:  # joining a running job costs nothing
            _limit(limiter, request, cost=GUIDE_COST)
        return {"job": engine.start_guide(repo_id).ref()}

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
    def create_run(body: CreateRunBody, request: Request) -> dict[str, Any]:
        question = body.question.strip()
        if not question:
            raise HTTPException(400, "Type a question first.")
        if store.get_repo(body.repo_id) is None:
            raise HTTPException(400, "This repository is not open. Open it from the home page.")
        ok, hint = ready()
        if not ok:
            raise HTTPException(503, hint)
        _limit(limiter, request)
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
        """Static files; pre-rendered public pages as directory indexes (/how-it-works ->
        how-it-works/index.html); the app shell (app.html) for /app and the legacy /repo and
        /run paths; the pre-rendered 404.html (status 404) for any other unknown path, or
        index.html when the build has no 404 page."""
        index = static / "index.html"
        if not index.is_file():
            return HTMLResponse(NOT_BUILT, status_code=503)
        root = static.resolve()
        page = {"Cache-Control": "no-cache"}
        clean = path.strip("/")
        if clean:
            target = (static / clean).resolve()
            if target.is_relative_to(root):
                if target.is_file():
                    headers = {"Cache-Control": "public, max-age=31536000, immutable"} if (
                        clean.startswith("assets/")) else {}
                    return FileResponse(target, headers=headers)
                if target.is_dir() and (target / "index.html").is_file():
                    return FileResponse(target / "index.html", headers=page)
                html = target.with_name(target.name + ".html")
                if html.is_file() and html.is_relative_to(root):
                    return FileResponse(html, headers=page)
        if _is_app_path(clean):
            app_html = static / "app.html"
            return FileResponse(app_html if app_html.is_file() else index, headers=page)
        not_found = static / "404.html"
        if clean and not_found.is_file():
            return FileResponse(not_found, status_code=404, headers=page)
        return FileResponse(index, headers=page)

    return app


OPEN_PATHS = {"/api/status", "/api/health"}
GUIDE_COST = 3  # a field guide counts as this many questions against the rate limit


def _is_app_path(path: str) -> bool:
    first = path.split("/", 1)[0]
    return first == "app" or (first in ("repo", "run") and "/" in path)


def _limit(limiter: _RateLimiter, request: Request, cost: int = 1) -> None:
    """429 when the caller's address is over its hourly limit (questions, suggestions,
    guides share it)."""
    wait_s = limiter.hit(_client_address(request), cost=cost)
    if wait_s:
        minutes = max(1, math.ceil(wait_s / 60))
        raise HTTPException(429, f"Too many questions from your address. Try again in "
                                 f"{minutes} minute{'s' if minutes != 1 else ''}.")


class _RateLimiter:
    """At most `per_hour` hits per key in any rolling hour (0 = unlimited)."""

    def __init__(self, per_hour: int):
        self.per_hour = max(0, per_hour)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, cost: int = 1) -> float:
        """Record `cost` hits; returns 0 if allowed, else seconds until they would be."""
        if not self.per_hour:
            return 0.0
        cost = max(1, min(cost, self.per_hour))
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= 3600:
                hits.popleft()
            if len(hits) + cost > self.per_hour:
                return 3600 - (now - hits[len(hits) + cost - self.per_hour - 1])
            hits.extend([now] * cost)
            return 0.0


def _client_address(request: Request) -> str:
    """The caller's address. Behind a hosting proxy the last X-Forwarded-For entry is the one
    the proxy added (earlier entries come from the client and can be forged)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


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
