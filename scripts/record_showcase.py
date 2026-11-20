"""Record real investigations for the public site's examples.

Opens each repository on a running Repo Archaeologist server, asks the question, waits for
the answer, and writes frontend/public/showcase/<slug>.json (ShowcaseRecording) plus
index.json (ShowcaseIndex), the shapes in frontend/src/api/types.ts. Recordings are replayed
on the public site with no API calls, so every number shown there comes from these files.

    uv run python scripts/record_showcase.py --server http://127.0.0.1:8790 spec.json

spec.json: [{"slug": "...", "repo": "owner/name", "language": "Python", "question": "..."}]
Uses real LLM calls (one investigation per entry). Never records access codes or keys.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "frontend" / "public" / "showcase"
TEXT_CAP = 1500  # answer evidence text kept per find (the replay shows excerpts)


def follow(client: httpx.Client, path: str, label: str) -> dict[str, Any]:
    """Follow an SSE stream to its terminal status event; returns that event."""
    last: dict[str, Any] = {}
    with client.stream("GET", path, timeout=None) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event.get("type") == "progress":
                print(f"  {label}: {event.get('message')}", file=sys.stderr)
            elif event.get("type") == "step":
                print(f"  {label}: {event['at']:6.1f}s {event['node']:<14} {event['summary'][:90]}",
                      file=sys.stderr)
            elif event.get("type") == "log":
                print(f"  {label}: note: {event['message'][:100]}", file=sys.stderr)
            if event.get("type") == "status" and event.get("status") in ("done", "error"):
                last = event
    return last


def trim(run: dict[str, Any]) -> dict[str, Any]:
    """Drop full texts from step events and cap answer evidence texts."""
    for event in run["events"]:
        for find in event.get("evidence") or []:
            find["text"] = None
        answer = event.get("answer")
        if answer:
            for find in answer["evidence"]:
                if find.get("text") and len(find["text"]) > TEXT_CAP:
                    find["text"] = find["text"][:TEXT_CAP] + "\n…"
    if run.get("answer"):
        for find in run["answer"]["evidence"]:
            if find.get("text") and len(find["text"]) > TEXT_CAP:
                find["text"] = find["text"][:TEXT_CAP] + "\n…"
    return run


def commit_count(path: str) -> int | None:
    try:
        out = subprocess.run(["git", "-C", path, "rev-list", "--count", "HEAD"],
                             capture_output=True, text=True, check=True, timeout=60)
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def record(client: httpx.Client, spec: dict[str, str]) -> dict[str, Any] | None:
    slug = spec["slug"]
    print(f"== {slug}: opening {spec['repo']}", file=sys.stderr)
    opened = client.post("/api/repos", json={"source": spec["repo"]})
    opened.raise_for_status()
    body = opened.json()
    done = follow(client, f"/api/jobs/{body['job']['id']}/events", slug)
    if done.get("status") != "done":
        print(f"  {slug}: could not open: {done.get('error')}", file=sys.stderr)
        return None
    repo = done["repo"]
    created = client.post("/api/runs", json={"repo_id": body["repo_id"], "question": spec["question"]})
    created.raise_for_status()
    run_id = created.json()["id"]
    finished = follow(client, f"/api/runs/{run_id}/events", slug)
    if finished.get("status") != "done":
        print(f"  {slug}: the question failed: {finished.get('error')}", file=sys.stderr)
        return None
    run = trim(client.get(f"/api/runs/{run_id}").json())
    return {
        "slug": slug,
        "repo": {
            "slug": spec["repo"],
            "web_url": repo.get("web_url") or f"https://github.com/{spec['repo']}",
            "language": spec.get("language", ""),
            "commits": commit_count(repo["path"]),
        },
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "run": run,
    }


def write_index() -> None:
    recordings = []
    for path in sorted(OUT.glob("*.json")):
        if path.name == "index.json":
            continue
        rec = json.loads(path.read_text())
        recordings.append({"slug": rec["slug"], "repo": rec["repo"],
                           "question": rec["run"]["question"], "recorded_at": rec["recorded_at"]})
    (OUT / "index.json").write_text(json.dumps({"recordings": recordings}, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("spec", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--access", default=None, help="access code, if the server needs one")
    args = parser.parse_args()
    headers = {"X-Access-Code": args.access} if args.access else {}
    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0
    with httpx.Client(base_url=args.server, headers=headers, timeout=60) as client:
        for spec in json.loads(args.spec.read_text()):
            rec = record(client, spec)
            if rec is None:
                failures += 1
                continue
            (OUT / f"{spec['slug']}.json").write_text(json.dumps(rec, indent=1) + "\n")
            answer = rec["run"]["answer"]
            print(f"  saved {spec['slug']}: {len(answer['citations'])} cited, {answer['hops']} hops, "
                  f"{rec['run']['elapsed_s']}s", file=sys.stderr)
    write_index()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
