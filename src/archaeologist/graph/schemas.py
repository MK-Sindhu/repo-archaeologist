"""Structured-output schemas the agent asks the LLM for.

Kept deliberately simple so the Anthropic structured-outputs API accepts them: no
dict fields, no numeric bounds, enums as Literal[...], only small nested models.
FakeLLM keys its responders by these class names, so the names are part of the API.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SearchSource = Literal["code", "docs", "commits", "prs"]
QuestionType = Literal["what", "where", "why", "when", "who", "how"]


class Target(BaseModel):
    """A code location the question names or implies."""

    path: str | None = Field(default=None, description="Repo-relative file path, if known.")
    symbol: str | None = Field(
        default=None, description="Function/class/method name, e.g. 'backoff' or 'Client.send'."
    )


class SourceQuery(BaseModel):
    """One search query aimed at one retrieval source."""

    source: SearchSource
    query: str = Field(description="Search text phrased for this source.")


class Analysis(BaseModel):
    """Planner output: what kind of question this is and how to search for it."""

    question_type: QuestionType
    targets: list[Target] = Field(default_factory=list)
    queries: list[SourceQuery] = Field(default_factory=list)


class Grade(BaseModel):
    """Relevance of one evidence item to the question."""

    id: str = Field(description="The evidence id exactly as shown.")
    score: Literal[0, 1, 2, 3] = Field(
        description="0 irrelevant, 1 tangential, 2 relevant, 3 directly answers the question."
    )
    reason: str = Field(default="", description="One short sentence.")


class Grades(BaseModel):
    """Grader output for one batch of evidence."""

    grades: list[Grade] = Field(default_factory=list)
    follow: list[str] = Field(
        default_factory=list,
        description="Canonical refs (e.g. 'pr:12', 'issue:7') worth fetching to learn why.",
    )


class Draft(BaseModel):
    """Synthesizer output: the cited answer."""

    answer: str = Field(description="Markdown answer citing evidence inline as [<evidence id>].")
    cited_ids: list[str] = Field(default_factory=list, description="Every evidence id cited.")
    confidence: Literal["high", "medium", "low"]
    missing: list[str] = Field(
        default_factory=list,
        description="Parts of the question the evidence does not establish.",
    )


class ClaimCheck(BaseModel):
    """Verifier verdict on one factual claim of the draft."""

    claim: str
    cited_ids: list[str] = Field(default_factory=list)
    supported: bool
    note: str = ""


class Verification(BaseModel):
    """Verifier output: per-claim checks plus searches that could support weak claims."""

    claims: list[ClaimCheck] = Field(default_factory=list)
    follow_up_queries: list[SourceQuery] = Field(default_factory=list)
