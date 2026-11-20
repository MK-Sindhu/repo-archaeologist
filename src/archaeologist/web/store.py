"""SQLite persistence for the web app: repositories and question runs (with their events)."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT,
    path TEXT NOT NULL,
    web_url TEXT,
    added_at TEXT NOT NULL,
    indexed_at TEXT,
    head TEXT,
    counts TEXT,
    shallow INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    options TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_s REAL,
    confidence TEXT,
    citations INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    events TEXT NOT NULL DEFAULT '[]',
    answer TEXT
);
CREATE INDEX IF NOT EXISTS runs_repo ON runs (repo_id, created_at);
CREATE TABLE IF NOT EXISTS artifacts (
    repo_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    head TEXT,
    version INTEGER NOT NULL,
    source TEXT,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (repo_id, kind)
);
"""

RUN_FIELDS = (
    "id", "repo_id", "question", "status", "options", "model", "created_at", "finished_at",
    "elapsed_s", "confidence", "citations", "error",
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            # runs that were in flight when the server stopped can never finish
            self._db.execute(
                "UPDATE runs SET status='error', error=? WHERE status IN ('queued','running')",
                ("The server stopped before this question finished. Ask it again.",),
            )
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- repos --------------------------------------------------------------------------
    def upsert_repo(self, repo: dict[str, Any]) -> None:
        row = {**repo, "counts": json.dumps(repo.get("counts")), "shallow": int(bool(repo.get("shallow")))}
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        updates = ", ".join(f"{k}=excluded.{k}" for k in row if k not in ("id", "added_at"))
        with self._lock:
            self._db.execute(
                f"INSERT INTO repos ({cols}) VALUES ({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
                row,
            )
            self._db.commit()

    def touch_repo(self, repo_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE repos SET last_used_at=? WHERE id=?", (now_iso(), repo_id))
            self._db.commit()

    def get_repo(self, repo_id: str) -> dict[str, Any] | None:
        rows = self._repos("WHERE r.id = ?", (repo_id,))
        return rows[0] if rows else None

    def list_repos(self) -> list[dict[str, Any]]:
        return self._repos("", ())

    def delete_repo(self, repo_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM repos WHERE id=?", (repo_id,))
            self._db.execute("DELETE FROM runs WHERE repo_id=?", (repo_id,))
            self._db.execute("DELETE FROM artifacts WHERE repo_id=?", (repo_id,))
            self._db.commit()
            return cur.rowcount > 0

    def _repos(self, where: str, args: tuple) -> list[dict[str, Any]]:
        sql = f"""
            SELECT r.*, COUNT(u.id) AS runs, MAX(u.created_at) AS last_run_at
            FROM repos r LEFT JOIN runs u ON u.repo_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY MAX(COALESCE(MAX(u.created_at), ''), r.last_used_at) DESC
        """
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["counts"] = json.loads(d["counts"]) if d.get("counts") else None
            d["shallow"] = bool(d["shallow"])
            d.pop("last_used_at", None)
            out.append(d)
        return out

    # -- runs ---------------------------------------------------------------------------
    def save_run(self, run: dict[str, Any], events: list[dict[str, Any]] | None = None,
                 answer: dict[str, Any] | None = None) -> None:
        row = {k: run.get(k) for k in RUN_FIELDS}
        row["options"] = json.dumps(run["options"])
        row["events"] = json.dumps(events or [])
        row["answer"] = json.dumps(answer) if answer is not None else None
        cols = ", ".join(row)
        marks = ", ".join(f":{k}" for k in row)
        with self._lock:
            self._db.execute(f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({marks})", row)
            self._db.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["options"] = json.loads(d["options"])
        d["events"] = json.loads(d["events"] or "[]")
        d["answer"] = json.loads(d["answer"]) if d["answer"] else None
        return d

    def list_runs(self, repo_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        cols = ", ".join(RUN_FIELDS)
        sql = f"SELECT {cols} FROM runs"
        args: tuple = ()
        if repo_id:
            sql += " WHERE repo_id = ?"
            args = (repo_id,)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*args, limit)).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["options"] = json.loads(d["options"])
            out.append(d)
        return out

    # -- artifacts (survey, suggestions, guide: per repository, per HEAD) ------------------
    def put_artifact(self, repo_id: str, kind: str, *, head: str | None, version: int,
                     data: dict[str, Any], source: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO artifacts (repo_id, kind, head, version, source, data, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (repo_id, kind, head, int(version), source, json.dumps(data), now_iso()),
            )
            self._db.commit()

    def get_artifact(self, repo_id: str, kind: str) -> dict[str, Any] | None:
        """{head, version, source, data, created_at} or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT head, version, source, data, created_at FROM artifacts "
                "WHERE repo_id=? AND kind=?", (repo_id, kind),
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["data"] = json.loads(d["data"])
        return d

    def valid_artifact(self, repo_id: str, kind: str, *, head: str | None,
                       version: int) -> dict[str, Any] | None:
        """The artifact's data when it was made for `head` (full sha) with `version`."""
        art = self.get_artifact(repo_id, kind)
        if art is None or art["version"] != version or not head or art["head"] != head:
            return None
        return art["data"]

    def delete_artifact(self, repo_id: str, kind: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM artifacts WHERE repo_id=? AND kind=?", (repo_id, kind))
            self._db.commit()
