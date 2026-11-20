"""Cross-module data models. Lower layers (git, github, index) may define their own
internal models, but everything that crosses a module boundary uses these."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["code", "commit", "pr", "issue", "doc", "comment"]


class Evidence(BaseModel):
    """One retrieved artifact the agent can cite.

    `id` is a canonical ref (see refs.py): "commit:<sha>", "pr:<n>", "issue:<n>",
    "code:<path>#L<a>-L<b>", "doc:<path>#<slug>". Citations in answers use these ids.
    `refs` lists canonical refs this item points to (PR in a squash subject, issues a PR
    closes, ...). The multi-hop step follows them.
    """

    id: str
    kind: EvidenceKind
    title: str
    text: str
    url: str | None = None
    source: str = ""  # which retriever/tool produced it, e.g. "search:code", "history", "fetch"
    score: float = 0.0
    date: str | None = None  # ISO-8601
    refs: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def render(self, max_chars: int = 2000) -> str:
        """Compact text block for prompts."""
        head = f"[{self.id}] ({self.kind}) {self.title}"
        if self.date:
            head += f" — {self.date[:10]}"
        body = self.text if len(self.text) <= max_chars else self.text[:max_chars] + "\n…[truncated]"
        tail = f"\nlinks: {', '.join(self.refs)}" if self.refs else ""
        return f"{head}\n{body}{tail}"


class Chunk(BaseModel):
    """A unit stored in the local hybrid index."""

    id: str  # canonical ref, same scheme as Evidence.id
    kind: Literal["code", "doc", "commit"]
    title: str
    text: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    symbol: str | None = None  # qualified name for code chunks, e.g. "Client.send"
    date: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class Hit(BaseModel):
    chunk: Chunk
    score: float
    bm25_rank: int | None = None
    dense_rank: int | None = None


class Answer(BaseModel):
    question: str
    answer: str  # markdown; cites evidence inline as [<evidence id>]
    citations: list[str] = Field(default_factory=list)  # evidence ids actually cited
    evidence: list[Evidence] = Field(default_factory=list)  # everything the answer may cite
    # ids the model cited that matched no evidence (removed from `citations` and the text);
    # kept so citation validity can count them against the system
    dropped_citations: list[str] = Field(default_factory=list)
    # every evidence id the system retrieved, before any grading/filtering ([] = `evidence`)
    retrieved_ids: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)
    hops: int = 0
    trace: list[str] = Field(default_factory=list)  # human-readable step log
    usage: dict[str, int] = Field(default_factory=dict)
    system: str = "agent"  # which system produced it (agent / baseline name / ablation)
    # "why": dug through history; "explain": explained the code as it is now (what/where/how
    # questions, and repositories with too little history to dig through)
    mode: str = "why"
    latency_s: float | None = None


class BenchmarkItem(BaseModel):
    """One eval question mined from a merged PR."""

    id: str
    repo: str  # "owner/name"
    question: str
    reference_rationale: str  # the "why", from PR body + linked issues (never shown to systems)
    target_path: str | None = None
    target_symbol: str | None = None
    gold_pr: int | None = None
    gold_commits: list[str] = Field(default_factory=list)
    gold_issues: list[int] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
