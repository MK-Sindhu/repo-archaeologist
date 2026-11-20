"""Translate agent state updates into the JSON events the web UI renders
(see frontend/src/api/types.ts for the contract)."""

from __future__ import annotations

from typing import Any

from ..graph.graph import summarize_update
from ..graph.state import is_partial
from ..models import Answer, Evidence

EXCERPT_CHARS = 600
FULL_CHARS = 6000
META_KEYS = ("path", "start_line", "end_line", "symbol", "author", "short_sha", "shallow_boundary",
             "state", "merged")


def find_payload(item: Evidence, *, full: bool = False) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "date": item.date,
        "score": item.score,
        "refs": list(item.refs),
        "excerpt": item.text[:EXCERPT_CHARS],
        "text": item.text[:FULL_CHARS] if full else None,
        "partial": is_partial(item),
        "meta": {k: item.meta[k] for k in META_KEYS if item.meta.get(k) is not None},
    }


def step_event(node: str, update: Any, at: float) -> dict[str, Any] | None:
    """One `step` event for a node's state update (None for non-dict updates)."""
    if not isinstance(update, dict):
        return None
    ev: dict[str, Any] = {
        "type": "step",
        "node": node,
        "at": round(at, 1),
        "summary": summarize_update(node, update),
    }
    analysis = update.get("analysis")
    if analysis is not None:
        ev["analysis"] = {
            "question_type": analysis.question_type,
            "targets": [{"path": t.path, "symbol": t.symbol} for t in analysis.targets],
            "queries": [{"source": q.source, "query": q.query} for q in analysis.queries],
        }
    if node == "retrieve":
        for key in update.get("searched") or []:
            source, _, query = key.partition("\x1f")
            ev["source"], ev["query"] = source, query
    evidence = update.get("evidence")
    if evidence:
        ev["evidence"] = [find_payload(e) for e in evidence.values()]
    grades = update.get("grades")
    if grades:
        ev["grades"] = [
            {"id": eid, "score": g.score, "reason": g.reason}
            for eid, g in grades.items() if g is not None
        ]
    if "kept" in update:
        ev["kept"] = list(update["kept"])
    if node == "grade" and "frontier" in update:
        ev["frontier"] = list(update["frontier"])
    if node == "follow" and "hops" in update:
        ev["hops"] = update["hops"]
    draft = update.get("draft")
    if draft is not None:
        ev["draft"] = {
            "confidence": draft.confidence,
            "cited_ids": list(draft.cited_ids),
            "missing": list(draft.missing),
        }
    if node == "verify":
        ev["unsupported"] = list(update.get("unsupported") or [])
        ev["retrying"] = bool(update.get("queries"))
    return ev


def answer_payload(answer: Answer, draft: Any, model: str) -> dict[str, Any]:
    return {
        "answer": answer.answer,
        "citations": list(answer.citations),
        "dropped_citations": list(answer.dropped_citations),
        "unsupported_claims": list(answer.unsupported_claims),
        "confidence": getattr(draft, "confidence", None),
        "missing": list(getattr(draft, "missing", []) or []),
        "evidence": [find_payload(e, full=True) for e in answer.evidence],
        "retrieved": len(answer.retrieved_ids) or len(answer.evidence),
        "hops": answer.hops,
        "usage": dict(answer.usage),
        "latency_s": answer.latency_s,
        "model": model,
    }
