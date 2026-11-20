"""Background work for the web app: clone + index jobs and question runs, each publishing
events to a Channel that any number of SSE streams can follow (replay, then live)."""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import GraphConfig, LLMConfig
from ..contracts import LLM
from ..graph.graph import build_graph, run_config
from ..sources.git import GitRepo
from ..toolbox import RepoToolbox, open_toolbox
from .events import answer_payload, step_event
from .sources import Source, SourceError, fetch, repo_id_for
from .store import Store, now_iso

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
    ):
        self.store = store
        self.llm_factory = llm_factory
        self.model_name = model_name
        self.toolbox_opener = toolbox_opener
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
            job.status = "done"
            ch.publish({"type": "status", "status": "done", "error": None,
                        "repo": self.repo_summary(job.repo_id)})
        except SourceError as exc:
            self._fail_job(job, str(exc))
        except Exception as exc:  # noqa: BLE001
            log.debug("job %s failed:\n%s", job.id, traceback.format_exc())
            self._fail_job(job, _friendly(exc))
        finally:
            self._deactivate(ch)

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


def _clones_root() -> Path:
    from .sources import clones_dir

    return clones_dir().resolve()


def _stage_label(stage: str, done: int, total: int | None) -> str:
    names = {"files": "Indexing files", "commits": "Indexing commits", "embed": "Embedding",
             "clone": "Cloning", "pull": "Updating"}
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

