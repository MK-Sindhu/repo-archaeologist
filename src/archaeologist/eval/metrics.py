"""Per-answer metrics, aggregation and cost estimates for the benchmark.

Gold references are matched with `refs.same_ref`, so an abbreviated commit SHA matches the
full one and `gh:<n>` matches `pr:<n>` / `issue:<n>`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ..models import Answer, BenchmarkItem
from ..refs import commit_ref, issue_ref, pr_ref, same_ref

# USD per million tokens, keyed by exact model id.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}


def gold_refs(item: BenchmarkItem) -> list[str]:
    """Canonical refs for the item's gold PR, merge commit(s) and linked issues."""
    out: list[str] = []
    if item.gold_pr is not None:
        out.append(pr_ref(item.gold_pr))
    out.extend(commit_ref(sha) for sha in item.gold_commits if sha)
    out.extend(issue_ref(n) for n in item.gold_issues)
    return out


def _matches_gold(item: BenchmarkItem, ids: Iterable[str]) -> bool:
    gold = gold_refs(item)
    return any(same_ref(i, g) for i in ids for g in gold)


def gold_hit_cited(item: BenchmarkItem, answer: Answer) -> bool:
    """True if the answer cites the gold PR, a gold commit or a gold issue.
    An item without gold refs never counts as a hit."""
    return _matches_gold(item, answer.citations)


def gold_hit_retrieved(item: BenchmarkItem, answer: Answer) -> bool:
    """True if any gold artifact is among everything the system retrieved (cited or not):
    `answer.retrieved_ids` plus the evidence ids. For the agent that includes items its grader
    later dropped, so the metric measures retrieval the same way for every system."""
    return _matches_gold(item, [*answer.retrieved_ids, *(e.id for e in answer.evidence)])


def citation_validity(answer: Answer) -> float:
    """Fraction of the ids the system cited that resolve to evidence it actually retrieved.

    Systems remove citations that match no evidence before building `answer.citations` and
    record them in `answer.dropped_citations`; those count as invalid here, so a system that
    invents ids scores below 1.0. An answer that cites nothing scores 0.0, not 1.0: validity is
    credit for grounded citing, so a system cannot earn a perfect score by citing nothing.
    Abbreviated SHAs and `gh:<n>` citations count as valid when they match an evidence id
    under `same_ref`.
    """
    total = len(answer.citations) + len(answer.dropped_citations)
    if not total:
        return 0.0
    ev_ids = [e.id for e in answer.evidence]
    valid = sum(1 for c in answer.citations if any(same_ref(c, e) for e in ev_ids))
    return valid / total


def estimate_cost(usage: Mapping[str, int], model: str) -> float | None:
    """USD cost of `usage` ({"input_tokens", "output_tokens", ...}) at `model`'s list price.
    Returns None for a model id not in PRICES."""
    price = PRICES.get(model)
    if price is None:
        return None
    tokens_in = usage.get("input_tokens", 0) or 0
    tokens_out = usage.get("output_tokens", 0) or 0
    return (tokens_in * price["input"] + tokens_out * price["output"]) / 1_000_000


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-system summary of result rows (as written by `run.run_eval`), in first-seen order.

    Quality metrics (gold_cited, gold_retrieved, judge_mean, citation_validity) average over
    every row, so a system error counts as a miss. judge_mean skips rows whose judge call
    failed (judge_score None). hops_mean and latency_mean_s average over rows without a
    system error. Tokens and cost are totals; cost_usd is None when no row had a price.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["system"], []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for system, rs in groups.items():
        ok = [r for r in rs if not r.get("error")]
        scores = [float(r["judge_score"]) for r in rs if r.get("judge_score") is not None]
        latencies = [float(r["latency_s"]) for r in ok if r.get("latency_s") is not None]
        costs = [float(r["cost_usd"]) for r in rs if r.get("cost_usd") is not None]
        usage = [r.get("usage") or {} for r in rs]
        out[system] = {
            "n": len(rs),
            "errors": len(rs) - len(ok),
            "gold_cited": _mean([float(bool(r.get("gold_hit_cited"))) for r in rs]),
            "gold_retrieved": _mean([float(bool(r.get("gold_hit_retrieved"))) for r in rs]),
            "judge_mean": _mean(scores),
            "judged": len(scores),
            "citation_validity": _mean([float(r.get("citation_validity") or 0.0) for r in rs]),
            "hops_mean": _mean([float(r.get("hops") or 0) for r in ok]),
            "latency_mean_s": _mean(latencies),
            "input_tokens": sum(int(u.get("input_tokens", 0) or 0) for u in usage),
            "output_tokens": sum(int(u.get("output_tokens", 0) or 0) for u in usage),
            "cost_usd": sum(costs) if costs else None,
        }
    return out
