"""Background work for the web app: clone + index jobs and question runs, each publishing
events to a Channel that any number of SSE streams can follow (replay, then live)."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import GraphConfig, LLMConfig
from ..contracts import LLM
from ..graph.graph import build_graph, run_config
from ..sources.git import GitRepo
from ..toolbox import RepoToolbox, open_toolbox
from . import guide as guide_mod
from . import suggestions as sugg
from .events import answer_payload, step_event
from .sources import Source, SourceError, fetch, repo_id_for
from .store import Store, now_iso
from .survey import SURVEY_VERSION, compute_survey

log = logging.getLogger(__name__)

TERMINAL = ("done", "error")
PING_S = 15.0


class Channel:
    """Append-only event list; readers block until new events arrive or it closes."""

    def __init__(self, events: list[dict[str, Any]] | None = None, closed: bool = False):
        self.events: list[dict[str, Any]] = list(events or [])
        self.closed = closed
        self._cond = threading.Condition()

    def publish(self, event: dict[str, Any]) -> None:
        with self._cond:
            if self.closed:
                return
            self.events.append(event)
            if event.get("type") == "status" and event.get("status") in TERMINAL:
                self.closed = True
            self._cond.notify_all()

    def read(self, start: int, timeout: float) -> tuple[list[dict[str, Any]], bool]:
        with self._cond:
            if start >= len(self.events) and not self.closed:
                self._cond.wait(timeout)
            return self.events[start:], self.closed

    def sse(self, start: int = 0, ping_s: float = PING_S) -> Iterator[str]:
        """Server-Sent Events: replay from `start`, then follow until closed."""
        import json

        i = max(0, start)
        while True:
            batch, closed = self.read(i, ping_s)
            for event in batch:
                yield f"id: {i}\ndata: {json.dumps(event, default=str)}\n\n"
                i += 1
            if closed and i >= len(self.events):
                return
            if not batch:
                yield ": ping\n\n"


@dataclass
class JobState:
    id: str
    kind: str
    repo_id: str
    channel: Channel = field(default_factory=Channel)
    status: str = "queued"
    error: str | None = None

    def ref(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "status": self.status}

    def view(self) -> dict[str, Any]:
        return {**self.ref(), "repo_id": self.repo_id, "error": self.error,
                "events": list(self.channel.events)}


@dataclass
class RunState:
    record: dict[str, Any]
    channel: Channel = field(default_factory=Channel)
    answer: dict[str, Any] | None = None
    started: float | None = None

    def at(self) -> float:
        return round(time.monotonic() - self.started, 1) if self.started else 0.0


class _LogRelay(logging.Handler):
    """Forwards archaeologist.* warnings (e.g. Gemini retries) to the live channels."""

    def __init__(self, engine: Engine):
        super().__init__(level=logging.WARNING)
        self.engine = engine

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return
        level = "error" if record.levelno >= logging.ERROR else "warning"
        self.engine._broadcast_log(level, message)


class Engine:
    def __init__(
        self,
        store: Store,
        llm_factory: Callable[[], LLM],
        model_name: Callable[[], str],
        *,
        max_runs: int = 1,
        toolbox_opener: Callable[..., RepoToolbox] = open_toolbox,
        llm_ready: Callable[[], bool] | None = None,
    ):
        self.store = store
        self.llm_factory = llm_factory
        self.model_name = model_name
        self.toolbox_opener = toolbox_opener
        self.llm_ready = llm_ready or (lambda: True)
        self._closed = False
        self._bg: list[threading.Thread] = []
        self._flight: dict[tuple[str, str], threading.Lock] = {}
        self._git: dict[tuple[str, str], GitRepo] = {}
        self._writing: set[str] = set()  # repos whose suggestions an LLM is writing
        self.jobs: dict[str, JobState] = {}
        self.runs: dict[str, RunState] = {}
        self._run_slots = threading.Semaphore(max(1, max_runs))
        self._lock = threading.Lock()
        self._repo_locks: dict[str, threading.Lock] = {}
        self._toolboxes: dict[tuple[str, bool], RepoToolbox] = {}
        self._active: set[Channel] = set()
        self._relay = _LogRelay(self)
        logging.getLogger("archaeologist").addHandler(self._relay)

    def close(self) -> None:
        self._closed = True
        logging.getLogger("archaeologist").removeHandler(self._relay)
        with self._lock:
            toolboxes, self._toolboxes = list(self._toolboxes.values()), {}
        for tb in toolboxes:
            try:
                tb.close()
            except Exception:
                log.debug("closing a toolbox failed", exc_info=True)

    # -- repositories -------------------------------------------------------------------
    def add_repo(self, source: Source) -> JobState:
        repo_id = repo_id_for(source.path)
        if self.store.get_repo(repo_id) is None:
            self.store.upsert_repo(self._repo_row(repo_id, source, indexed=None))
        else:
            self.store.touch_repo(repo_id)
        return self._start_job("add_repo", repo_id, source)

    def reindex(self, repo_id: str) -> JobState | None:
        repo = self.store.get_repo(repo_id)
        if repo is None:
            return None
        kind = "github" if repo["slug"] and Path(repo["path"]).is_relative_to(_clones_root()) else "local"
        source = Source(kind, Path(repo["path"]), repo["slug"])
        return self._start_job("reindex", repo_id, source)

    def forget_repo(self, repo_id: str) -> bool:
        with self._lock:
            for key in [k for k in self._toolboxes if k[0] == repo_id]:
                self._toolboxes.pop(key).close()
        return self.store.delete_repo(repo_id)

    def _start_job(self, kind: str, repo_id: str, source: Source) -> JobState:
        job = JobState(id=uuid.uuid4().hex[:12], kind=kind, repo_id=repo_id)
        with self._lock:
            self.jobs[job.id] = job
        threading.Thread(target=self._job_main, args=(job, source), daemon=True,
                         name=f"job-{job.id}").start()
        return job

    def _job_main(self, job: JobState, source: Source) -> None:
        ch = job.channel
        job.status = "running"
        self._activate(ch)
        ch.publish({"type": "status", "status": "running"})
        last = [0.0, ""]

        def progress(stage: str, done: int, total: int | None, message: str = "") -> None:
            now = time.monotonic()
            if stage == last[1] and now - last[0] < 0.25 and (total is None or done < total):
                return
            last[0], last[1] = now, stage
            ch.publish({"type": "progress", "stage": stage, "done": done, "total": total,
                        "message": message or _stage_label(stage, done, total)})

        try:
            with self._repo_lock(job.repo_id):
                fetch(source, progress, lambda m: ch.publish(
                    {"type": "log", "level": "warning", "message": m}))
                progress("files", 0, None, "Indexing")
                toolbox = self.toolbox_opener(
                    source.path, offline=False, progress=lambda s, d, t: progress(s, d, t)
                )
                self._replace_toolbox(job.repo_id, False, toolbox)
                row = self._repo_row(job.repo_id, source, indexed=toolbox)
                self.store.upsert_repo(row)
            self._after_index(job.repo_id, toolbox.repo, progress)
            job.status = "done"
            ch.publish({"type": "status", "status": "done", "error": None,
                        "repo": self.repo_summary(job.repo_id)})
            self._maybe_write_suggestions(job.repo_id)
        except SourceError as exc:
            self._fail_job(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.debug("job %s failed:\n%s", job.id, traceback.format_exc())
            self._fail_job(job, _friendly(exc))
        finally:
            self._deactivate(ch)

    # -- after indexing: survey and template suggestions (no LLM) -------------------------
    def _after_index(self, repo_id: str, repo: GitRepo, progress: Callable[..., None]) -> None:
        """Survey and template suggestions, stored as artifacts for the new HEAD. Failures are
        logged, not fatal: both are recomputed on demand."""
        try:
            progress("survey", 0, None, "Surveying the history")
            survey = compute_survey(repo)
            self.store.put_artifact(repo_id, "survey", head=survey["head"],
                                    version=SURVEY_VERSION, data=survey)
        except Exception:
            log.warning("survey of %s failed", repo_id, exc_info=True)
            return
        try:
            progress("suggest", 0, None, "Suggesting questions")
            self._store_template_suggestions(repo_id, repo, survey)
        except Exception:
            log.warning("suggestions for %s failed", repo_id, exc_info=True)

    def _store_template_suggestions(self, repo_id: str, repo: GitRepo,
                                    survey: dict[str, Any]) -> dict[str, Any]:
        files = [f[0] for f in survey["files"]]
        commits = int(survey["totals"]["commits"])
        slug = repo.remote_slug()
        candidates = (sugg.find_candidates(repo, set(files), owner=slug[0] if slug else None,
                                           name=slug[1] if slug else None)
                      if commits > sugg.THIN_HISTORY else [])
        data = {
            "status": "pending" if self.llm_ready() else "ready",
            "source": "template",
            "head": survey["head"],
            "items": sugg.template_suggestions(files, commits, candidates),
            "candidates": [asdict(c) for c in candidates],
            "commits": commits,
        }
        self.store.put_artifact(repo_id, "suggestions", head=survey["head"],
                                version=sugg.SUGGESTIONS_VERSION, source="template", data=data)
        return data

    def _fail_job(self, job: JobState, message: str) -> None:
        job.status, job.error = "error", message
        job.channel.publish({"type": "status", "status": "error", "error": message, "repo": None})
        repo = self.store.get_repo(job.repo_id)
        if repo is not None and repo["indexed_at"] is None and job.kind == "add_repo":
            self.store.delete_repo(job.repo_id)  # never opened successfully: don't list it

    def _repo_row(self, repo_id: str, source: Source, indexed: RepoToolbox | None) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": repo_id,
            "name": source.path.name.split("__")[-1] if source.slug else source.path.name,
            "slug": source.slug,
            "path": str(source.path),
            "web_url": f"https://github.com/{source.slug}" if source.slug else None,
            "added_at": now_iso(),
            "indexed_at": None,
            "head": None,
            "counts": None,
            "shallow": False,
            "last_used_at": now_iso(),
        }
        if indexed is not None:
            repo: GitRepo = indexed.repo
            slug = source.slug or ("/".join(s) if (s := repo.remote_slug()) else None)
            head = repo.head_sha()
            row.update(
                slug=slug,
                web_url=f"https://github.com/{slug}" if slug else None,
                indexed_at=now_iso(),
                head=head[:12] if head else None,
                counts={k: indexed.index.count(k) for k in ("code", "doc", "commit")},
                shallow=repo.is_shallow,
            )
            existing = self.store.get_repo(repo_id)
            if existing:
                row["added_at"] = existing["added_at"]
        return row

    def repo_summary(self, repo_id: str) -> dict[str, Any] | None:
        return self.store.get_repo(repo_id)

    # -- runs ---------------------------------------------------------------------------
    def start_run(self, repo_id: str, question: str, options: dict[str, bool]) -> RunState:
        record = {
            "id": uuid.uuid4().hex[:12],
            "repo_id": repo_id,
            "question": question,
            "status": "queued",
            "options": options,
            "model": self.model_name(),
            "created_at": now_iso(),
            "finished_at": None,
            "elapsed_s": None,
            "confidence": None,
            "citations": 0,
            "error": None,
        }
        run = RunState(record=record)
        run.channel.publish({"type": "status", "status": "queued", "at": 0.0})
        with self._lock:
            self.runs[record["id"]] = run
        self.store.save_run(record, run.channel.events)
        self.store.touch_repo(repo_id)
        threading.Thread(target=self._run_main, args=(run,), daemon=True,
                         name=f"run-{record['id']}").start()
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            live = self.runs.get(run_id)
        if live is not None:
            return {**live.record, "events": list(live.channel.events), "answer": live.answer}
        return self.store.get_run(run_id)

    def run_channel(self, run_id: str) -> Channel | None:
        with self._lock:
            live = self.runs.get(run_id)
        if live is not None:
            return live.channel
        stored = self.store.get_run(run_id)
        if stored is None:
            return None
        return Channel(stored["events"], closed=True)

    def _run_main(self, run: RunState) -> None:
        rec, ch = run.record, run.channel
        with self._run_slots:
            run.started = time.monotonic()
            rec["status"] = "running"
            ch.publish({"type": "status", "status": "running", "at": 0.0})
            self._activate(ch)
            try:
                self._execute(run)
                rec.update(status="done", finished_at=now_iso(), elapsed_s=run.at())
                ch.publish({"type": "status", "status": "done", "at": run.at()})
            except Exception as exc:  # noqa: BLE001
                log.debug("run %s failed:\n%s", rec["id"], traceback.format_exc())
                message = _friendly(exc)
                rec.update(status="error", error=message, finished_at=now_iso(), elapsed_s=run.at())
                ch.publish({"type": "status", "status": "error", "at": run.at(), "error": message})
            finally:
                self._deactivate(ch)
                self.store.save_run(rec, ch.events, run.answer)
                with self._lock:
                    self.runs.pop(rec["id"], None)

    def _execute(self, run: RunState) -> None:
        rec, ch = run.record, run.channel
        opts = rec["options"]
        repo = self.store.get_repo(rec["repo_id"])
        if repo is None:
            raise SourceError("This repository was removed. Open it again to ask questions.")
        toolbox = self._toolbox(rec["repo_id"], Path(repo["path"]), offline=bool(opts["offline"]))
        llm = self.llm_factory()
        llm_config = getattr(llm, "config", None)
        config = GraphConfig(multi_hop=bool(opts["follow_links"]), verify=bool(opts["verify"]))
        graph = build_graph(toolbox, llm, config,
                            llm_config if isinstance(llm_config, LLMConfig) else None)
        draft = None
        for chunk in graph.stream({"question": rec["question"]}, config=run_config(config),
                                  stream_mode="updates"):
            for node, update in chunk.items():
                event = step_event(node, update, run.at())
                if event is None:
                    continue
                if node == "synthesize":
                    draft = update.get("draft")
                if node == "finalize":
                    answer = update["answer"]
                    run.answer = answer_payload(answer, draft, rec["model"])
                    event["answer"] = run.answer
                    rec["confidence"] = run.answer["confidence"]
                    rec["citations"] = len(answer.citations)
                ch.publish(event)
        if run.answer is None:
            raise RuntimeError("the agent finished without an answer")

    # -- toolboxes ----------------------------------------------------------------------
    def _toolbox(self, repo_id: str, path: Path, *, offline: bool) -> RepoToolbox:
        key = (repo_id, offline)
        with self._lock:
            cached = self._toolboxes.get(key)
        if cached is not None:
            return cached
        with self._repo_lock(repo_id):
            with self._lock:
                cached = self._toolboxes.get(key)
            if cached is not None:
                return cached
            toolbox = self.toolbox_opener(path, offline=offline)
            self._replace_toolbox(repo_id, offline, toolbox)
            return toolbox

    def _replace_toolbox(self, repo_id: str, offline: bool, toolbox: RepoToolbox) -> None:
        with self._lock:
            self._toolboxes[(repo_id, offline)] = toolbox
            if not offline:  # an online re-index makes the offline copy stale too
                self._toolboxes.pop((repo_id, True), None)

    def _repo_lock(self, repo_id: str) -> threading.Lock:
        with self._lock:
            return self._repo_locks.setdefault(repo_id, threading.Lock())

    # -- repository helpers for the read endpoints ---------------------------------------
    def repo_busy(self, repo_id: str) -> bool:
        """An open or re-index job for the repository is queued or running."""
        with self._lock:
            jobs = list(self.jobs.values())
        return any(j.repo_id == repo_id and j.kind in ("add_repo", "reindex")
                   and j.status in ("queued", "running") for j in jobs)

    def git_repo(self, repo_id: str) -> GitRepo | None:
        repo = self.store.get_repo(repo_id)
        if repo is None:
            return None
        key = (repo_id, repo["path"])
        with self._lock:
            cached = self._git.get(key)
        if cached is not None:
            return cached
        try:
            git = GitRepo(repo["path"])
        except (ValueError, RuntimeError):
            return None
        with self._lock:
            self._git[key] = git
        return git

    def read_toolbox(self, repo_id: str) -> RepoToolbox | None:
        """A toolbox for read endpoints: the cached one, or one opened WITHOUT rebuilding the
        index (so a request never starts an index build)."""
        with self._lock:
            cached = self._toolboxes.get((repo_id, False)) or self._toolboxes.get((repo_id, True))
        if cached is not None:
            return cached
        repo = self.store.get_repo(repo_id)
        if repo is None:
            return None
        with self._repo_lock(repo_id):
            with self._lock:
                cached = self._toolboxes.get((repo_id, False))
            if cached is not None:
                return cached
            toolbox = self.toolbox_opener(Path(repo["path"]), offline=False, build=False)
            with self._lock:
                self._toolboxes.setdefault((repo_id, False), toolbox)
                return self._toolboxes[(repo_id, False)]

    def flight(self, repo_id: str, kind: str) -> threading.Lock:
        """One lock per (repo, artifact): on-demand work runs once, concurrent callers wait."""
        with self._lock:
            return self._flight.setdefault((repo_id, kind), threading.Lock())

    def ensure_survey(self, repo_id: str) -> dict[str, Any] | None:
        """The survey for the current HEAD, computing it once if missing or stale."""
        git = self.git_repo(repo_id)
        if git is None:
            return None
        head = git.head_sha()
        data = self.store.valid_artifact(repo_id, "survey", head=head, version=SURVEY_VERSION)
        if data is not None:
            return data
        with self.flight(repo_id, "survey"):
            data = self.store.valid_artifact(repo_id, "survey", head=head, version=SURVEY_VERSION)
            if data is None:
                data = compute_survey(git)
                self.store.put_artifact(repo_id, "survey", head=data["head"],
                                        version=SURVEY_VERSION, data=data)
        return data

    def ensure_suggestions(self, repo_id: str) -> dict[str, Any] | None:
        """Stored suggestions for the current HEAD (templates computed once if missing)."""
        git = self.git_repo(repo_id)
        if git is None:
            return None
        head = git.head_sha()
        version = sugg.SUGGESTIONS_VERSION
        data = self.store.valid_artifact(repo_id, "suggestions", head=head, version=version)
        if data is not None:
            if (data.get("source") == "template" and data.get("status") == "ready"
                    and not data.get("llm_tried") and self.llm_ready()
                    and repo_id not in self._writing):
                # written without a key earlier; a key is available now
                data["status"] = "pending"
                self.store.put_artifact(repo_id, "suggestions", head=head, version=version,
                                        source="template", data=data)
                self._maybe_write_suggestions(repo_id)
            return data
        with self.flight(repo_id, "suggestions"):
            data = self.store.valid_artifact(repo_id, "suggestions", head=head, version=version)
            if data is None:
                survey = self.ensure_survey(repo_id)
                if survey is None:
                    return None
                data = self._store_template_suggestions(repo_id, git, survey)
        self._maybe_write_suggestions(repo_id)
        return data

    def refresh_suggestions(self, repo_id: str) -> dict[str, Any] | None:
        data = self.ensure_suggestions(repo_id)
        if data is None:
            return None
        if data.get("status") != "pending":
            data["status"] = "pending"
            self.store.put_artifact(repo_id, "suggestions", head=data["head"],
                                    version=sugg.SUGGESTIONS_VERSION,
                                    source=data.get("source"), data=data)
        self._maybe_write_suggestions(repo_id, force=True)
        return data

    def _maybe_write_suggestions(self, repo_id: str, *, force: bool = False) -> None:
        """Start the background LLM writer when the stored suggestions are pending."""
        if self._closed or not self.llm_ready():
            return
        art = self.store.get_artifact(repo_id, "suggestions")
        if art is None or (art["data"].get("status") != "pending" and not force):
            return
        with self._lock:
            if repo_id in self._writing:
                return
            self._writing.add(repo_id)
        thread = threading.Thread(target=self._write_suggestions, args=(repo_id,), daemon=True,
                                  name=f"suggest-{repo_id}")
        with self._lock:
            self._bg = [t for t in self._bg if t.is_alive()] + [thread]
        thread.start()

    def _write_suggestions(self, repo_id: str) -> None:
        try:
            with self._run_slots:
                if self._closed:
                    return
                art = self.store.get_artifact(repo_id, "suggestions")
                git = self.git_repo(repo_id)
                if art is None or git is None:
                    return
                data, head = art["data"], art["head"]
                survey = self.store.valid_artifact(repo_id, "survey", head=head,
                                                   version=SURVEY_VERSION)
                candidates = [sugg.Candidate(**c) for c in data.get("candidates", [])]
                readme = _readme_text(git, survey)
                repo = self.store.get_repo(repo_id) or {}
                try:
                    llm = self.llm_factory()
                    cfg = getattr(llm, "config", None)
                    items = sugg.write_suggestions(
                        llm, repo_name=repo.get("slug") or repo.get("name") or repo_id,
                        readme=readme, survey=survey, candidates=candidates,
                        commits=int(data.get("commits") or 0),
                        effort=getattr(cfg, "effort_analyze", None),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.info("writing suggestions for %s failed: %s", repo_id, _friendly(exc))
                    data["status"] = "failed"
                    data["error"] = _friendly(exc)
                else:
                    if items:
                        data.update(status="ready", source="llm", items=items)
                    else:
                        data["status"] = "ready"
                data["llm_tried"] = True
                current = self.store.get_artifact(repo_id, "suggestions")
                if current is None or current["head"] != head or self._closed:
                    return  # re-indexed or removed meanwhile
                self.store.put_artifact(repo_id, "suggestions", head=head,
                                        version=sugg.SUGGESTIONS_VERSION,
                                        source=data.get("source"), data=data)
        except sqlite3.ProgrammingError:
            pass  # the store closed underneath (server shutting down)
        except Exception:
            log.debug("suggestion writer for %s crashed", repo_id, exc_info=True)
        finally:
            with self._lock:
                self._writing.discard(repo_id)

    def join_background(self, timeout: float = 10.0) -> None:
        """Wait for background writers (tests)."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                alive = [t for t in self._bg if t.is_alive()]
                jobs = [j for j in self.jobs.values() if j.status in ("queued", "running")]
            if not alive and not jobs:
                return
            if time.monotonic() > deadline:
                return
            for t in alive:
                t.join(max(0.0, min(0.2, deadline - time.monotonic())))
            if jobs and not alive:
                time.sleep(0.02)

    # -- field guide (on request) ----------------------------------------------------------
    def guide_job(self, repo_id: str) -> JobState | None:
        with self._lock:
            jobs = list(self.jobs.values())
        return next((j for j in jobs if j.repo_id == repo_id and j.kind == "guide"
                     and j.status in ("queued", "running")), None)

    def start_guide(self, repo_id: str) -> JobState:
        running = self.guide_job(repo_id)
        if running is not None:
            return running
        job = JobState(id=uuid.uuid4().hex[:12], kind="guide", repo_id=repo_id)
        with self._lock:
            self.jobs[job.id] = job
        thread = threading.Thread(target=self._guide_main, args=(job,), daemon=True,
                                  name=f"guide-{job.id}")
        with self._lock:
            self._bg = [t for t in self._bg if t.is_alive()] + [thread]
        thread.start()
        return job

    def _guide_main(self, job: JobState) -> None:
        ch, repo_id = job.channel, job.repo_id
        head: str | None = None

        def stage(name: str, done: int, total: int | None, message: str) -> None:
            ch.publish({"type": "progress", "stage": name, "done": done, "total": total,
                        "message": message})

        try:
            with self._run_slots:
                job.status = "running"
                ch.publish({"type": "status", "status": "running"})
                self._activate(ch)
                git = self.git_repo(repo_id)
                repo = self.store.get_repo(repo_id)
                if git is None or repo is None:
                    raise SourceError("This repository was removed. Open it again first.")
                head = git.head_sha()
                stage("gather", 0, None, "Reading the README, the folders and the entry points")
                survey = self.ensure_survey(repo_id) or {}
                toolbox = self.read_toolbox(repo_id)
                if toolbox is None:
                    raise SourceError("This repository is not indexed yet. Open it again first.")
                slug = repo.get("slug")
                evidence, facts = guide_mod.gather(git, toolbox.index, survey, slug=slug, head=head)
                stage("write", 1, 3, "Writing the guide")
                llm = self.llm_factory()
                cfg = getattr(llm, "config", None)
                markdown = guide_mod.write_guide(
                    llm, repo_name=slug or repo.get("name") or repo_id, facts=facts,
                    evidence=evidence, effort=getattr(cfg, "effort_synthesize", None))
                stage("check", 2, 3, "Checking every citation")
                text, citations, finds = guide_mod.check_guide(markdown, evidence)
                data = {
                    "status": "ready", "head": head, "markdown": text, "citations": citations,
                    "evidence": finds, "generated_at": now_iso(),
                    "model": getattr(cfg, "model", None) or self.model_name(), "error": None,
                }
                self.store.put_artifact(repo_id, "guide", head=head,
                                        version=guide_mod.GUIDE_VERSION, data=data)
                stage("check", 3, 3, "Done")
            job.status = "done"
            ch.publish({"type": "status", "status": "done", "error": None, "repo": None})
        except Exception as exc:  # noqa: BLE001
            log.debug("guide job %s failed:\n%s", job.id, traceback.format_exc())
            message = _friendly(exc)
            job.status, job.error = "error", message
            try:
                self.store.put_artifact(repo_id, "guide", head=head,
                                        version=guide_mod.GUIDE_VERSION, data={
                                            "status": "failed", "head": head, "markdown": None,
                                            "citations": [], "evidence": [], "generated_at": None,
                                            "model": None, "error": message})
            except sqlite3.ProgrammingError:
                pass
            ch.publish({"type": "status", "status": "error", "error": message, "repo": None})
        finally:
            self._deactivate(ch)

    # -- log relay ----------------------------------------------------------------------
    def _activate(self, ch: Channel) -> None:
        with self._lock:
            self._active.add(ch)

    def _deactivate(self, ch: Channel) -> None:
        with self._lock:
            self._active.discard(ch)

    def _broadcast_log(self, level: str, message: str) -> None:
        with self._lock:
            channels = list(self._active)
            runs = {id(r.channel): r for r in self.runs.values()}
        for ch in channels:
            event: dict[str, Any] = {"type": "log", "level": level, "message": message}
            run = runs.get(id(ch))
            if run is not None:
                event["at"] = run.at()
            ch.publish(event)


def _readme_text(git: GitRepo, survey: dict[str, Any] | None) -> str:
    names = [f[0] for f in (survey or {}).get("files", []) if "/" not in f[0]]
    readme = next((n for n in sorted(names) if n.lower().startswith("readme")), None)
    return (git.read_file(readme) or "")[:4000] if readme else ""


def _clones_root() -> Path:
    from .sources import clones_dir

    return clones_dir().resolve()


def _stage_label(stage: str, done: int, total: int | None) -> str:
    names = {"files": "Indexing files", "commits": "Indexing commits", "embed": "Embedding",
             "clone": "Cloning", "pull": "Updating", "survey": "Surveying the history",
             "suggest": "Suggesting questions"}
    label = names.get(stage, stage.capitalize())
    if total:
        return f"{label} {done:,}/{total:,}"
    return f"{label} {done:,}" if done else label


def _friendly(exc: BaseException) -> str:
    """A user-facing message for an exception raised by a job or run."""
    from ..llm import LLMAuthError, LLMError

    try:
        import anthropic
    except ImportError:  # pragma: no cover
        anthropic = None  # type: ignore[assignment]
    if isinstance(exc, (LLMAuthError, SourceError)):
        return str(exc)
    if anthropic is not None:
        if isinstance(exc, anthropic.AuthenticationError) or (
            isinstance(exc, TypeError) and "authentication" in str(exc).lower()
        ):
            from ..backends import ANTHROPIC_HINT

            return "Claude rejected the API key. " + ANTHROPIC_HINT.split(". ", 1)[1]
        if isinstance(exc, anthropic.APIError):
            return f"Claude API error: {exc}"
    if isinstance(exc, LLMError):
        return str(exc)
    if isinstance(exc, ValueError):
        return str(exc)
    return f"Unexpected error ({exc.__class__.__name__}): {exc}"

