"""Run systems over a benchmark, judge every answer, and write results + a report.

out_dir/results.jsonl  one row per (item, system) run; appended, so re-running skips pairs that
                       already succeeded and retries pairs whose last run failed (the latest
                       row of a pair is the one that counts)
out_dir/summary.json   per-system aggregates (metrics.aggregate) for this run's items/systems
out_dir/report.md      markdown table of the summary plus a short guide to reading it

Systems see only `item.question`: never gold ids, the PR title or the reference rationale.
Items and systems run sequentially (systems may parallelise internally), so the usage deltas
taken around each judge call are exact.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import DEFAULT_MODEL
from ..contracts import LLM
from ..models import Answer, BenchmarkItem
from .judge import judge
from .metrics import (
    aggregate,
    citation_validity,
    estimate_cost,
    gold_hit_cited,
    gold_hit_retrieved,
)
from .mine import append_jsonl, read_jsonl

System = Callable[[str], Answer]


def _usage_delta(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {k: v - before.get(k, 0) for k, v in after.items()}


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _evaluate(
    item: BenchmarkItem,
    name: str,
    system: System,
    judge_llm: LLM,
    model_for_cost: str,
    system_llm: LLM | None = None,
) -> dict[str, Any]:
    """Run one system on one item and build its result row. Never raises for system or judge
    failures: they are recorded in the row ("error" / "judge_error"). With `system_llm`, a
    failed run's usage is that LLM's usage delta around the call (tokens spent before the
    failure still count toward cost)."""
    row: dict[str, Any] = {"item_id": item.id, "system": name, "question": item.question}
    started = time.perf_counter()
    usage_before = system_llm.usage_snapshot() if system_llm is not None else None
    try:
        answer = system(item.question)
        if not isinstance(answer, Answer):
            raise TypeError(f"system returned {type(answer).__name__}, expected Answer")
    except Exception as exc:  # noqa: BLE001 - a failing system is a result, not a crash
        usage: dict[str, int] = {}
        if system_llm is not None and usage_before is not None:
            usage = _usage_delta(usage_before, system_llm.usage_snapshot())
        row.update(
            error=_error_text(exc),
            answer="",
            citations=[],
            dropped_citations=[],
            evidence_ids=[],
            retrieved_ids=[],
            unsupported_claims=[],
            trace=[],
            hops=0,
            latency_s=time.perf_counter() - started,
            usage=usage,
            cost_usd=estimate_cost(usage, model_for_cost) if usage else None,
            judge_score=0,
            judge_reason="system error; no answer to judge",
            judge_error=None,
            judge_usage={},
            gold_hit_cited=False,
            gold_hit_retrieved=False,
            citation_validity=0.0,
        )
        return row

    elapsed = time.perf_counter() - started
    row.update(
        error=None,
        answer=answer.answer,
        citations=answer.citations,
        dropped_citations=answer.dropped_citations,
        evidence_ids=[e.id for e in answer.evidence],
        retrieved_ids=answer.retrieved_ids,
        unsupported_claims=answer.unsupported_claims,
        trace=answer.trace,
        hops=answer.hops,
        latency_s=answer.latency_s if answer.latency_s is not None else elapsed,
        usage=answer.usage,
        cost_usd=estimate_cost(answer.usage, model_for_cost),
        gold_hit_cited=gold_hit_cited(item, answer),
        gold_hit_retrieved=gold_hit_retrieved(item, answer),
        citation_validity=citation_validity(answer),
    )
    judge_before = judge_llm.usage_snapshot()
    try:
        verdict = judge(item, answer, judge_llm)
        row.update(judge_score=verdict.score, judge_reason=verdict.reason, judge_error=None)
    except Exception as exc:  # noqa: BLE001 - a judge failure leaves the row unscored
        row.update(judge_score=None, judge_reason="", judge_error=_error_text(exc))
    row["judge_usage"] = _usage_delta(judge_before, judge_llm.usage_snapshot())
    return row


def _fmt(value: float | None, spec: str, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value:{spec}}{suffix}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


_TABLE_HEADER = (
    "| system | n | errors | gold PR/commit cited | gold retrieved | judge (0-2) "
    "| citation validity | mean hops | mean latency | tokens | est. cost |\n"
    "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
)

_HOW_TO_READ = """\
## How to read this

- **gold PR/commit cited**: share of questions where the answer cites the PR that introduced
  the code, its merge commit (or a branch commit it merged), or an issue the PR references
  (abbreviated SHAs and `gh:<n>` refs count). **gold retrieved**: the same artifacts appeared
  anywhere in what the system retrieved (for the agent, before grading), cited or not.
- **judge (0-2)**: an LLM compares the answer's stated reason with a reference rationale
  written from the PR and its issues (2 = key reason, 1 = partial, 0 = wrong or none). The
  judge never sees gold ids; rows whose judge call failed are left out of the mean.
- **citation validity**: share of the ids an answer cited that point at evidence the system
  actually retrieved. Invented ids are removed from the answer but still count against it
  here. An answer with no citations scores 0.
- Errors count as misses (and judge 0) in the quality columns; hops and latency average over
  runs without errors. Tokens and cost cover answering only; the judge used {judge_tokens:,}
  tokens on top.
- Questions were generated from the diff alone and filtered for wording shared with the PR,
  so finding the PR is part of the task.
"""


def render_report(summary: Mapping[str, Any]) -> str:
    """Markdown report for a summary produced by `run_eval`."""
    n = summary["n_items"]
    meta = (
        f"{n} item{'' if n == 1 else 's'} · generated {summary['generated_at']} · cost "
        f"estimated at `{summary['model_for_cost']}` list prices"
    )
    lines = ["# Repo Archaeologist benchmark", "", meta, "", _TABLE_HEADER]
    for name, s in summary["systems"].items():
        tokens = s["input_tokens"] + s["output_tokens"]
        lines.append(
            f"| {name} | {s['n']} | {s['errors']} | {_pct(s['gold_cited'])} "
            f"| {_pct(s['gold_retrieved'])} | {_fmt(s['judge_mean'], '.2f')} "
            f"| {_pct(s['citation_validity'])} | {_fmt(s['hops_mean'], '.1f')} "
            f"| {_fmt(s['latency_mean_s'], '.1f', ' s')} | {tokens:,} | {_usd(s['cost_usd'])} |"
        )
    judge_usage = summary.get("judge_usage") or {}
    judge_tokens = judge_usage.get("input_tokens", 0) + judge_usage.get("output_tokens", 0)
    lines += ["", _HOW_TO_READ.format(judge_tokens=judge_tokens)]
    return "\n".join(lines)


def run_eval(
    items: Sequence[BenchmarkItem],
    systems: Mapping[str, System],
    judge_llm: LLM,
    out_dir: str | Path,
    model_for_cost: str = DEFAULT_MODEL,
    on_row: Callable[[dict[str, Any]], None] | None = None,
    *,
    system_llm: LLM | None = None,
    retry_errors: bool = True,
) -> dict[str, Any]:
    """Evaluate every system on every item; write results.jsonl, summary.json and report.md.

    Each system is called as `system(item.question)`. Exceptions from a system are recorded
    in its row (and count as a miss); a failing judge leaves the row unscored. Pairs
    (item_id, system) that already succeeded in results.jsonl are skipped, so an interrupted
    run resumes without duplicating them; pairs whose latest row is a system error are run
    again unless `retry_errors=False` (a transient outage should not become a permanent
    miss). `system_llm` is the LLM the systems use: when given, a failed run's row still
    records the tokens it spent. `on_row` is called with each newly written row (progress).
    The summary covers this call's items and systems, including rows from earlier runs (the
    latest row of each pair). Returns the summary dict (also written to summary.json).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    latest: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in read_jsonl(results_path):
        latest[(r.get("item_id"), r.get("system"))] = r
    done = {key for key, r in latest.items() if not (retry_errors and r.get("error"))}

    for item in items:
        for name, system in systems.items():
            if (item.id, name) in done:
                continue
            row = _evaluate(item, name, system, judge_llm, model_for_cost, system_llm)
            append_jsonl(results_path, row)
            done.add((item.id, name))
            if on_row is not None:
                on_row(row)

    item_ids = {i.id for i in items}
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in read_jsonl(results_path):
        key = (r.get("item_id"), r.get("system"))
        if key[0] in item_ids and key[1] in systems:
            rows_by_key[key] = r  # appended in run order: the latest row of a pair wins
    per_system = aggregate(rows_by_key.values())

    judge_usage: dict[str, int] = {}
    for r in rows_by_key.values():
        for k, v in (r.get("judge_usage") or {}).items():
            judge_usage[k] = judge_usage.get(k, 0) + int(v)

    summary: dict[str, Any] = {
        "n_items": len(items),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model_for_cost": model_for_cost,
        "systems": {name: per_system[name] for name in systems if name in per_system},
        "judge_usage": judge_usage,
    }
    (out / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (out / "report.md").write_text(render_report(summary), encoding="utf-8")
    return summary
