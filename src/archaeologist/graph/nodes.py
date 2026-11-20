"""Node and router functions of the Repo Archaeologist graph.

    analyze -> retrieve (Send per source) -> trace_history -> grade -> follow? -> grade ...
            -> synthesize -> verify -> (retry: retrieve ...) -> finalize

Every trace line is "<node>: <message>", which `graph.summarize_update` relies on.
Toolbox errors never end a run: they are recorded in the trace and the node carries on.
LLM errors propagate (ClaudeLLM already retries transient API failures).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NamedTuple, TypeVar

from langgraph.types import Send

from ..config import GraphConfig, LLMConfig
from ..contracts import LLM, SEARCH_SOURCES, Toolbox
from ..models import Answer, Evidence
from ..refs import parse_ref, same_ref
from . import prompts
from .schemas import Analysis, Draft, Grade, Grades, SourceQuery, Target, Verification
from .state import AgentState, RetrieveTask, add_unique, is_partial, merge_evidence

KEEP_SCORE = 2  # grades below this are dropped
FOLLOWABLE_KINDS = ("commit", "pr", "issue")  # kinds whose refs feed the frontier
HISTORY_TOP_CODE_HITS = 3  # code hits whose history trace_history takes
MAX_FOLLOW_UP_QUERIES = 4  # per verify retry
MAX_WORKERS = 8  # thread pool size for parallel tool / LLM calls inside one node

GRADE_ITEM_CHARS = 1_200  # per-item text budget when grading
GRADE_BATCH_ITEMS = 12
GRADE_BATCH_CHARS = 16_000
PROMPT_EVIDENCE_CHARS = 60_000  # total evidence budget for synthesize / verify prompts
PROMPT_ITEM_CHARS = 3_000
PROMPT_MIN_ITEM_CHARS = 300

_CODE_ID = re.compile(r"^code:(?P<path>.+)#L(?P<start>\d+)-L(?P<end>\d+)$")
_MARKER = re.compile(r"([ \t]?)\[([^\[\]\n]{1,400})\](?!\()")

X = TypeVar("X")
Y = TypeVar("Y")


# -- small helpers -------------------------------------------------------------------


def _parallel(fn: Callable[[X], Y], items: Sequence[X]) -> list[Y]:
    """Map `fn` over `items`, concurrently when there are several; results keep input order."""
    if len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as pool:
        return list(pool.map(fn, items))


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _count(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else plural or noun + 's'}"


def _clip(text: str, n: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def short_ref(ref: str) -> str:
    """Trace-friendly ref: commit SHAs abbreviated to 7 characters."""
    kind, _, value = ref.partition(":")
    return f"commit:{value[:7]}" if kind == "commit" else ref


def _ref_list(refs: Iterable[str], limit: int = 6) -> str:
    refs = list(refs)
    shown = ", ".join(short_ref(r) for r in refs[:limit])
    return shown + (f" (+{len(refs) - limit} more)" if len(refs) > limit else "")


def is_ref(text: str) -> bool:
    """True when `text` is a single canonical ref such as 'pr:12' (no whitespace)."""
    if not text or any(ch.isspace() for ch in text):
        return False
    try:
        parse_ref(text)
    except ValueError:
        return False
    return True


def search_key(source: str, query: str) -> str:
    """Identity of one search, used to avoid running the same query twice."""
    return f"{source}\x1f{' '.join(query.lower().split())}"


def _with_source(item: Evidence, source: str) -> Evidence:
    return item if item.source else item.model_copy(update={"source": source})


def _pool(items: Iterable[Evidence], source: str) -> dict[str, Evidence]:
    pool: dict[str, Evidence] = {}
    for item in items:
        pool = merge_evidence(pool, {item.id: _with_source(item, source)})
    return pool


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _symbol_matches(a: str | None, b: str | None) -> bool:
    """'Client.send' matches 'send' and vice versa."""
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


class HistoryJob(NamedTuple):
    """One toolbox.history call."""

    path: str
    start_line: int | None
    end_line: int | None
    symbol: str | None

    @property
    def key(self) -> str:
        return f"{self.path}#L{self.start_line}-L{self.end_line}@{self.symbol}"

    @property
    def label(self) -> str:
        span = f" L{self.start_line}-{self.end_line}" if self.start_line is not None else ""
        sym = f" ({self.symbol})" if self.symbol else ""
        return f"{self.path}{span}{sym}"


def code_location(item: Evidence) -> HistoryJob | None:
    """Where a code hit lives, from Evidence.meta (path/start_line/end_line/symbol), falling
    back to parsing a 'code:<path>#L<a>-L<b>' id."""
    meta = item.meta
    path = meta.get("path")
    start, end = _as_int(meta.get("start_line")), _as_int(meta.get("end_line"))
    if not path:
        m = _CODE_ID.match(item.id)
        if m is None:
            return None
        path, start, end = m["path"], int(m["start"]), int(m["end"])
    symbol = meta.get("symbol")
    return HistoryJob(str(path), start, end, str(symbol) if symbol else None)


def _clean_targets(targets: Iterable[Target]) -> list[Target]:
    out: list[Target] = []
    seen: set[tuple[str | None, str | None]] = set()
    for t in targets:
        path = (t.path or "").strip().removeprefix("./").removeprefix("/") or None
        symbol = (t.symbol or "").strip() or None
        if (path or symbol) and (path, symbol) not in seen:
            seen.add((path, symbol))
            out.append(Target(path=path, symbol=symbol))
    return out


def _canonical(ref: str, known: Sequence[str]) -> str | None:
    if ref in known:
        return ref
    return next((k for k in known if same_ref(ref, k)), None)


def resolve_citations(
    text: str, cited_ids: Sequence[str], known_ids: Sequence[str]
) -> tuple[str, list[str], list[str]]:
    """Validate an answer's citations against the evidence it may cite.

    Bracket markers whose contents are all canonical refs ("[pr:12]", "[pr:12, issue:7]")
    are citation markers: ids not in `known_ids` are removed from them (a marker left empty
    is removed with its leading space) and ids that match a known id by `same_ref` are
    rewritten to that id. Other brackets (markdown links, "[1]", "[note]") are untouched.

    Returns (cleaned text, citations, dropped ids). The inline markers are the source of
    truth: citations are the known ids that appear in a marker, in order of first appearance,
    whether or not the model also listed them in `cited_ids` (models often leave that list
    incomplete). Unknown ids from either the markers or `cited_ids` are dropped.
    """
    known = list(dict.fromkeys(known_ids))
    in_text: list[str] = []
    dropped: list[str] = []

    def fix(m: re.Match[str]) -> str:
        lead, body = m.group(1), m.group(2)
        parts = [p.strip().strip("`").strip() for p in re.split(r"[,;]", body)]
        if not all(is_ref(p) for p in parts):
            return m.group(0)
        good: list[str] = []
        for part in parts:
            canon = _canonical(part, known)
            if canon is None:
                dropped.append(part)
            elif canon not in good:
                good.append(canon)
        in_text.extend(good)
        return f"{lead}[{', '.join(good)}]" if good else ""

    cleaned = _MARKER.sub(fix, text)
    for ref in cited_ids:
        if _canonical(ref.strip(), known) is None:
            dropped.append(ref.strip())
    citations = list(dict.fromkeys(in_text))
    return cleaned, citations, list(dict.fromkeys(d for d in dropped if d))


def listed_not_inline(
    cited_ids: Sequence[str], citations: Sequence[str], known_ids: Sequence[str]
) -> list[str]:
    """Known ids the model listed in `cited_ids` but never cited inline (a consistency note)."""
    known = list(dict.fromkeys(known_ids))
    listed = (_canonical(ref.strip(), known) for ref in cited_ids)
    return [eid for eid in dict.fromkeys(e for e in listed if e) if eid not in citations]


# -- nodes ---------------------------------------------------------------------------


class AgentNodes:
    """The graph's node and router callables, bound to one toolbox, LLM and configuration.

    Instances keep no per-run state, so the concurrent `retrieve` tasks of a run (and
    concurrent runs) can share one instance. The toolbox and LLM must be thread-safe.
    """

    def __init__(self, toolbox: Toolbox, llm: LLM, config: GraphConfig, llm_config: LLMConfig):
        unknown = [s for s in config.sources if s not in SEARCH_SOURCES and s != "history"]
        if unknown:
            allowed = ", ".join([*SEARCH_SOURCES, "history"])
            raise ValueError(f"unknown sources {unknown}; expected a subset of: {allowed}")
        self.toolbox = toolbox
        self.llm = llm
        self.config = config
        self.llm_config = llm_config
        self.search_sources = [s for s in dict.fromkeys(config.sources) if s in SEARCH_SOURCES]
        self.use_history = "history" in config.sources

    # analyze ------------------------------------------------------------------------

    def analyze(self, state: AgentState) -> dict[str, Any]:
        """Classify the question, name code targets and plan one query per enabled source;
        sources the model gives no query for fall back to the raw question."""
        question = state["question"]
        started_at = state.get("started_at") or time.time()
        usage_start = state.get("usage_start") or self.llm.usage_snapshot()
        raw = self.llm.structured(
            system=prompts.ANALYZE_SYSTEM,
            prompt=prompts.analyze_prompt(question, self.search_sources, self.use_history),
            schema=Analysis,
            effort=self.llm_config.effort_analyze,
            role="analyze",
        )
        planned: dict[str, str] = {}
        for q in raw.queries:
            text = q.query.strip()
            if q.source in self.search_sources and text and q.source not in planned:
                planned[q.source] = text
        fallback = [s for s in self.search_sources if s not in planned]
        queries = [
            SourceQuery(source=s, query=planned.get(s, question)) for s in self.search_sources
        ]
        targets = _clean_targets(raw.targets)
        analysis = raw.model_copy(update={"targets": targets, "queries": queries})

        parts = [f"{analysis.question_type} question"]
        if targets:
            names = [":".join(x for x in (t.path, t.symbol) if x) for t in targets]
            parts.append("targets " + ", ".join(names))
        if planned:
            parts.append(
                "queries " + ", ".join(f"{s}={_clip(q, 40)!r}" for s, q in planned.items())
            )
        if fallback:
            parts.append("raw question for " + ", ".join(fallback))
        return {
            "started_at": started_at,
            "usage_start": usage_start,
            "analysis": analysis,
            "queries": queries,
            "hops": state.get("hops", 0),
            "retries": state.get("retries", 0),
            "trace": ["analyze: " + "; ".join(parts)],
        }

    def route_after_analyze(self, state: AgentState) -> list[Send] | str:
        """Fan out one retrieve task per planned query (straight to trace_history if none)."""
        return self._fan_out(state.get("queries", [])) or "trace_history"

    # retrieve -----------------------------------------------------------------------

    def retrieve(self, task: RetrieveTask) -> dict[str, Any]:
        """Run one search (one Send task per source; tasks run concurrently)."""
        source, query = task["source"], task["query"]
        key = search_key(source, query)
        try:
            items = self.toolbox.search(source, query, k=self.config.k_per_source)
        except Exception as exc:  # noqa: BLE001 - a failing source must not end the run
            line = f"retrieve: {source} {_clip(query, 60)!r} failed ({_err(exc)})"
            return {"searched": [key], "trace": [line]}
        found = _pool(items, f"search:{source}")
        line = f"retrieve: {source} {_clip(query, 60)!r} -> {_count(len(found), 'hit')}"
        if found:
            line += f" ({_ref_list(found)})"
        return {"evidence": found, "searched": [key], "trace": [line]}

    def _fan_out(self, queries: Sequence[SourceQuery]) -> list[Send]:
        return [Send("retrieve", RetrieveTask(source=q.source, query=q.query)) for q in queries]

    # trace_history --------------------------------------------------------------------

    def trace_history(self, state: AgentState) -> dict[str, Any]:
        """Take `toolbox.history` for named targets and the top code hits not traced yet."""
        if not self.use_history:
            return {"trace": ["trace_history: skipped (history source disabled)"]}
        analysis = state.get("analysis")
        jobs, notes = self._history_jobs(
            analysis.targets if analysis else [],
            state.get("evidence", {}),
            set(state.get("traced", [])),
        )
        if not jobs:
            return {"trace": ["trace_history: nothing new to trace" + "".join(notes)]}

        def run(job: HistoryJob) -> tuple[list[Evidence], str | None]:
            try:
                items = self.toolbox.history(
                    job.path,
                    start_line=job.start_line,
                    end_line=job.end_line,
                    symbol=job.symbol,
                    k=self.config.k_per_source,
                )
            except Exception as exc:  # noqa: BLE001 - tool errors are recorded, not raised
                return [], _err(exc)
            return list(items), None

        pool: dict[str, Evidence] = {}
        lines: list[str] = []
        for job, (items, error) in zip(jobs, _parallel(run, jobs)):
            if error:
                lines.append(f"trace_history: {job.label} failed ({error})")
                continue
            found = _pool(items, "history")
            pool = merge_evidence(pool, found)
            detail = f" ({_ref_list(found)})" if found else ""
            lines.append(f"trace_history: {job.label} -> {_count(len(found), 'commit')}{detail}")
        lines[-1] += "".join(notes)
        return {"evidence": pool, "traced": [job.key for job in jobs], "trace": lines}

    def _history_jobs(
        self, targets: Sequence[Target], evidence: dict[str, Evidence], traced: set[str]
    ) -> tuple[list[HistoryJob], list[str]]:
        hits = sorted((e for e in evidence.values() if e.kind == "code"), key=lambda e: -e.score)
        locations = [loc for e in hits if (loc := code_location(e)) is not None]
        top = locations[:HISTORY_TOP_CODE_HITS]
        jobs: list[HistoryJob] = []
        notes: list[str] = []
        for t in targets:
            if t.path:
                covered = t.symbol and any(
                    j.path == t.path and _symbol_matches(j.symbol, t.symbol) for j in top
                )
                if not covered:
                    jobs.append(HistoryJob(t.path, None, None, t.symbol))
                continue
            match = next((j for j in locations if _symbol_matches(j.symbol, t.symbol)), None)
            if match is None:
                notes.append(f"; no code location found for symbol {t.symbol!r}")
            else:
                jobs.append(match)
        jobs.extend(top)
        unique = {job.key: job for job in jobs if job.key not in traced}
        return list(unique.values()), notes

    # grade ----------------------------------------------------------------------------

    def grade(self, state: AgentState) -> dict[str, Any]:
        """Grade evidence not graded before, keep score >= 2 (best first, capped at
        max_evidence) and compute the next hop's frontier."""
        question = state["question"]
        analysis = state.get("analysis")
        qtype = analysis.question_type if analysis else "why"
        evidence = state.get("evidence", {})
        graded = state.get("grades", {})
        new_items = [e for eid, e in evidence.items() if eid not in graded]

        new_grades: dict[str, Grade] = {}
        picks: list[str] = []
        batches = _grade_batches(new_items)
        for grades, follow in _parallel(lambda b: self._grade_batch(question, qtype, b), batches):
            new_grades.update(grades)
            picks.extend(follow)

        all_grades = {**graded, **new_grades}
        kept = self._rank(all_grades, evidence)
        suggested = add_unique(state.get("suggested", []), picks)
        frontier = self._frontier(suggested, kept, evidence, state.get("visited", []))

        if new_items:
            n_new_kept = sum(1 for eid in new_grades if eid in kept)
            line = (
                f"grade: {_count(len(new_items), 'new item')} in "
                f"{_count(len(batches), 'batch', 'batches')}, "
                f"{n_new_kept} kept; {len(kept)} kept in total"
            )
        else:
            line = f"grade: nothing new; {len(kept)} kept"
        if frontier:
            line += f"; frontier {_ref_list(frontier)}"
        return {
            "grades": new_grades,
            "kept": kept,
            "suggested": picks,
            "frontier": frontier,
            "trace": [line],
        }

    def _grade_batch(
        self, question: str, qtype: str, batch: list[tuple[Evidence, str]]
    ) -> tuple[dict[str, Grade], list[str]]:
        ids = [e.id for e, _ in batch]
        out = self.llm.structured(
            system=prompts.GRADE_SYSTEM,
            prompt=prompts.grade_prompt(question, qtype, "\n\n".join(b for _, b in batch)),
            schema=Grades,
            effort=self.llm_config.effort_grade,
            role="grade",
        )
        result: dict[str, Grade] = {}
        for g in out.grades:
            eid = _canonical(g.id.strip(), ids)
            if eid is not None and eid not in result:
                result[eid] = g.model_copy(update={"id": eid})
        for eid in ids:
            result.setdefault(eid, Grade(id=eid, score=0, reason="not graded by the model"))
        follow = [r.strip() for r in out.follow if is_ref(r.strip())]
        return result, follow

    def _rank(self, grades: dict[str, Grade], evidence: dict[str, Evidence]) -> list[str]:
        keep = [eid for eid, g in grades.items() if g.score >= KEEP_SCORE and eid in evidence]
        keep.sort(key=lambda eid: (-grades[eid].score, -evidence[eid].score))
        return keep[: max(self.config.max_evidence, 0)]

    def _frontier(
        self,
        suggested: Sequence[str],
        kept: Sequence[str],
        evidence: dict[str, Evidence],
        visited: Sequence[str],
    ) -> list[str]:
        """Grader picks first, then refs of kept commit/pr/issue items; minus refs already
        fetched or already in the evidence pool (by same_ref); at most max_follow_per_hop.
        A PR/issue known only from a search hit (no discussion yet) does not count as in the
        pool, so it can still be fetched in full."""
        cap = self.config.max_follow_per_hop
        if cap <= 0:
            return []
        candidates = list(suggested)
        for eid in kept:
            if evidence[eid].kind in FOLLOWABLE_KINDS:
                candidates.extend(evidence[eid].refs)
        known = [*visited, *(eid for eid, e in evidence.items() if not is_partial(e))]
        frontier: list[str] = []
        for ref in candidates:
            if not is_ref(ref) or any(same_ref(ref, k) for k in (*known, *frontier)):
                continue
            frontier.append(ref)
            if len(frontier) >= cap:
                break
        return frontier

    def route_after_grade(self, state: AgentState) -> str:
        """Follow refs while there is a frontier, multi-hop is on and hops remain."""
        if (
            self.config.multi_hop
            and state.get("frontier")
            and state.get("hops", 0) < self.config.max_hops
        ):
            return "follow"
        return "synthesize"

    # follow ---------------------------------------------------------------------------

    def follow(self, state: AgentState) -> dict[str, Any]:
        """Fetch the frontier refs (concurrently) and count one hop. A fetched item that
        replaces a partial search hit has its old grade removed so it is graded again."""
        refs = list(state.get("frontier", []))[: self.config.max_follow_per_hop]
        hop = state.get("hops", 0) + 1
        current = state.get("evidence", {})

        def fetch(ref: str) -> tuple[Evidence | None, str | None]:
            try:
                return self.toolbox.fetch(ref), None
            except Exception as exc:  # noqa: BLE001 - tool errors are recorded, not raised
                return None, _err(exc)

        pool: dict[str, Evidence] = {}
        visited = list(refs)
        got: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        regrade: dict[str, None] = {}
        for ref, (item, error) in zip(refs, _parallel(fetch, refs)):
            if error:
                failed.append(f"{ref} ({error})")
            elif item is None:
                missing.append(ref)
            else:
                item = _with_source(item, "fetch")
                pool = merge_evidence(pool, {item.id: item})
                visited.append(item.id)
                got.append(item.id)
                old = current.get(item.id)
                if old is not None and is_partial(old) and not is_partial(item):
                    regrade[item.id] = None
        line = f"follow: hop {hop} fetched {_ref_list(got) if got else 'nothing'}"
        if regrade:
            line += f" (full versions of search hits {_ref_list(regrade)})"
        if missing:
            line += f"; not found: {_ref_list(missing)}"
        if failed:
            line += "; failed: " + ", ".join(failed)
        update: dict[str, Any] = {
            "evidence": pool,
            "visited": visited,
            "hops": hop,
            "frontier": [],
            "trace": [line],
        }
        if regrade:
            update["grades"] = regrade
        return update

    # synthesize / verify ------------------------------------------------------------------

    def _kept(self, state: AgentState) -> list[Evidence]:
        evidence = state.get("evidence", {})
        return [evidence[eid] for eid in state.get("kept", []) if eid in evidence]

    def _blocks(self, items: Sequence[Evidence]) -> str:
        return prompts.render_blocks(
            items, PROMPT_EVIDENCE_CHARS, PROMPT_ITEM_CHARS, PROMPT_MIN_ITEM_CHARS
        )

    def synthesize(self, state: AgentState) -> dict[str, Any]:
        """Draft a cited answer from the kept evidence only."""
        kept = self._kept(state)
        if not kept:
            draft = Draft(
                answer=prompts.NO_EVIDENCE_ANSWER,
                confidence="low",
                missing=["no relevant evidence was found"],
            )
            return {"draft": draft, "trace": ["synthesize: no relevant evidence; nothing to cite"]}
        analysis = state.get("analysis")
        draft = self.llm.structured(
            system=prompts.SYNTHESIZE_SYSTEM,
            prompt=prompts.synthesize_prompt(
                state["question"],
                analysis.question_type if analysis else "why",
                self._blocks(kept),
                state.get("unsupported", []),
            ),
            schema=Draft,
            effort=self.llm_config.effort_synthesize,
            role="synthesize",
        )
        line = (
            f"synthesize: draft from {_count(len(kept), 'item')}, confidence {draft.confidence}, "
            f"cites {_ref_list(draft.cited_ids) if draft.cited_ids else 'nothing'}"
        )
        if draft.missing:
            line += f"; gaps: {_clip('; '.join(draft.missing), 120)}"
        return {"draft": draft, "trace": [line]}

    def verify(self, state: AgentState) -> dict[str, Any]:
        """Check each claim against the evidence; schedule a retry round with follow-up
        queries while unsupported claims remain and retries are left."""
        kept = self._kept(state)
        draft = state["draft"]
        if not kept:
            return {
                "unsupported": [],
                "queries": [],
                "trace": ["verify: skipped (no evidence to check against)"],
            }
        result = self.llm.structured(
            system=prompts.VERIFY_SYSTEM,
            prompt=prompts.verify_prompt(
                state["question"], draft.answer, self._blocks(kept), self.search_sources
            ),
            schema=Verification,
            effort=self.llm_config.effort_verify,
            role="verify",
        )
        unsupported = list(
            dict.fromkeys(
                c.claim.strip() for c in result.claims if not c.supported and c.claim.strip()
            )
        )
        retries = state.get("retries", 0)
        queries: list[SourceQuery] = []
        if unsupported and retries < self.config.max_verify_retries:
            queries = self._fresh_queries(result.follow_up_queries, state.get("searched", []))

        update: dict[str, Any] = {"unsupported": unsupported, "queries": queries}
        total = len(result.claims)
        if not unsupported:
            line = f"verify: {_count(total, 'claim')} checked, all supported"
        elif queries:
            update["retries"] = retries + 1
            line = (
                f"verify: {len(unsupported)}/{total} claims unsupported; retry "
                f"{retries + 1}/{self.config.max_verify_retries} with "
                + ", ".join(f"{q.source}={_clip(q.query, 40)!r}" for q in queries)
            )
        else:
            reason = (
                "no retries left"
                if retries >= self.config.max_verify_retries
                else "no new follow-up queries"
            )
            line = f"verify: {len(unsupported)}/{total} claims unsupported; {reason}"
        update["trace"] = [line]
        return update

    def _fresh_queries(
        self, proposed: Sequence[SourceQuery], searched: Sequence[str]
    ) -> list[SourceQuery]:
        done = set(searched)
        out: list[SourceQuery] = []
        for q in proposed:
            text = q.query.strip()
            key = search_key(q.source, text)
            if q.source in self.search_sources and text and key not in done:
                done.add(key)
                out.append(SourceQuery(source=q.source, query=text))
            if len(out) >= MAX_FOLLOW_UP_QUERIES:
                break
        return out

    def route_after_verify(self, state: AgentState) -> list[Send] | str:
        """Retry retrieval with the verifier's follow-up queries, or finish."""
        return self._fan_out(state.get("queries", [])) or "finalize"

    # finalize ---------------------------------------------------------------------------

    def finalize(self, state: AgentState) -> dict[str, Any]:
        """Build the Answer: validated citations (and the invented ones dropped), kept
        evidence (score = 0-3 grade, the retriever's score kept in meta['retrieval_score']),
        the ids of everything retrieved, trace, usage delta, latency."""
        draft = state["draft"]
        evidence = state.get("evidence", {})
        grades = state.get("grades", {})
        kept = [
            _graded(evidence[eid], grades[eid])
            for eid in state.get("kept", [])
            if eid in evidence and eid in grades
        ]
        kept.sort(key=lambda e: e.score, reverse=True)
        kept_ids = [e.id for e in kept]
        text, citations, dropped = resolve_citations(draft.answer, draft.cited_ids, kept_ids)
        not_inline = listed_not_inline(draft.cited_ids, citations, kept_ids)
        end = self.llm.usage_snapshot()
        start = state.get("usage_start", {})
        usage = {k: v - start.get(k, 0) for k, v in end.items()}
        started_at = state.get("started_at")
        latency = round(time.time() - started_at, 3) if started_at else None
        unsupported = list(state.get("unsupported", []))
        hops = state.get("hops", 0)

        line = (
            f"finalize: {_count(len(citations), 'citation')}, "
            f"{_count(len(kept), 'evidence item')}, {_count(hops, 'hop')}"
        )
        if dropped:
            line += f"; dropped unknown citations {_ref_list(dropped)}"
        if not_inline:
            line += f"; listed but not cited inline {_ref_list(not_inline)}"
        if unsupported:
            line += f"; {_count(len(unsupported), 'unsupported claim')}"
        answer = Answer(
            question=state["question"],
            answer=text,
            citations=citations,
            evidence=kept,
            dropped_citations=dropped,
            retrieved_ids=list(evidence),
            unsupported_claims=unsupported,
            hops=hops,
            trace=[*state.get("trace", []), line],
            usage=usage,
            system="agent",
            latency_s=latency,
        )
        return {"answer": answer, "trace": [line]}


def _graded(item: Evidence, grade: Grade) -> Evidence:
    meta = {**item.meta, "retrieval_score": item.score, "grade_reason": grade.reason}
    return item.model_copy(update={"score": float(grade.score), "meta": meta})


def _grade_batches(items: Sequence[Evidence]) -> list[list[tuple[Evidence, str]]]:
    """Rendered items split into batches bounded by item count and total characters."""
    batches: list[list[tuple[Evidence, str]]] = []
    current: list[tuple[Evidence, str]] = []
    size = 0
    for item in items:
        block = prompts.render_block(item, GRADE_ITEM_CHARS)
        if current and (len(current) >= GRADE_BATCH_ITEMS or size + len(block) > GRADE_BATCH_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append((item, block))
        size += len(block)
    if current:
        batches.append(current)
    return batches
