"""Graph assembly and the public entry points: build_graph, ask, iter_steps.

```
analyze ──► (Send ×N) retrieve[source] ──► trace_history ──► grade ──┐
                                                                     ▼
              ┌────────────── follow (fetch refs) ◄── route: frontier & hops < max
              ▼                                      │
            grade ─────────────────────────────────► synthesize ──► verify ──► finalize
                                                        ▲              │
                                                        └── retry ◄────┘
```
With `GraphConfig.verify=False` the verify node is left out (synthesize -> finalize).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from ..config import GraphConfig, LLMConfig
from ..contracts import LLM, Toolbox
from ..models import Answer
from .nodes import AgentNodes
from .state import AgentState

SUMMARY_CHARS = 240


def build_graph(
    toolbox: Toolbox,
    llm: LLM,
    config: GraphConfig | None = None,
    llm_config: LLMConfig | None = None,
) -> CompiledStateGraph:
    """Compile the agent graph. Invoke it with {"question": ...}; the final state's
    "answer" holds the Answer. Raises ValueError for unknown entries in config.sources.
    `llm_config` only supplies the per-role effort values."""
    config = config or GraphConfig()
    nodes = AgentNodes(toolbox, llm, config, llm_config or LLMConfig())
    graph = StateGraph(AgentState)
    graph.add_node("analyze", nodes.analyze)
    graph.add_node("retrieve", nodes.retrieve)
    graph.add_node("trace_history", nodes.trace_history)
    graph.add_node("grade", nodes.grade)
    graph.add_node("follow", nodes.follow)
    graph.add_node("synthesize", nodes.synthesize)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "analyze")
    graph.add_conditional_edges("analyze", nodes.route_after_analyze, ["retrieve", "trace_history"])
    graph.add_edge("retrieve", "trace_history")
    graph.add_edge("trace_history", "grade")
    graph.add_conditional_edges("grade", nodes.route_after_grade, ["follow", "synthesize"])
    graph.add_edge("follow", "grade")
    if config.verify:
        graph.add_node("verify", nodes.verify)
        graph.add_edge("synthesize", "verify")
        graph.add_conditional_edges("verify", nodes.route_after_verify, ["retrieve", "finalize"])
    else:
        graph.add_edge("synthesize", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(name="repo-archaeologist")


def run_config(config: GraphConfig | None = None) -> RunnableConfig:
    """Invocation config whose recursion limit covers the longest path `config` allows
    (every hop is follow + grade; every verify retry is retrieve .. verify again)."""
    config = config or GraphConfig()
    hops = max(config.max_hops, 0)
    retries = max(config.max_verify_retries, 0) if config.verify else 0
    return {"recursion_limit": 20 + 2 * hops + 6 * retries}


def _inputs(question: str) -> AgentState:
    question = question.strip()
    if not question:
        raise ValueError("question must not be empty")
    return {"question": question}


def ask(
    question: str,
    toolbox: Toolbox,
    llm: LLM,
    config: GraphConfig | None = None,
    llm_config: LLMConfig | None = None,
) -> Answer:
    """Answer one question with the full agent and return the cited Answer."""
    graph = build_graph(toolbox, llm, config, llm_config)
    state = graph.invoke(_inputs(question), config=run_config(config))
    return state["answer"]


def summarize_update(node: str, update: dict[str, Any]) -> str:
    """Short human-readable summary of one node's state update, built from its trace
    lines ("<node>: <message>")."""
    messages = [line.removeprefix(f"{node}: ") for line in update.get("trace") or []]
    text = "; ".join(messages) or "done"
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


class StepStream(Iterator[tuple[str, str]]):
    """Progress of one agent run as (node name, summary) pairs, in execution order.

    Iterating drives the run (parallel retrieve tasks each yield their own step). Once
    the stream is exhausted, `answer` holds the final Answer.
    """

    def __init__(self, graph: CompiledStateGraph, inputs: AgentState, config: RunnableConfig):
        self.answer: Answer | None = None
        self._steps = self._run(graph, inputs, config)

    def __next__(self) -> tuple[str, str]:
        return next(self._steps)

    def _run(
        self, graph: CompiledStateGraph, inputs: AgentState, config: RunnableConfig
    ) -> Iterator[tuple[str, str]]:
        for chunk in graph.stream(inputs, config=config, stream_mode="updates"):
            for node, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                if isinstance(update.get("answer"), Answer):
                    self.answer = update["answer"]
                yield node, summarize_update(node, update)


def iter_steps(
    question: str,
    toolbox: Toolbox,
    llm: LLM,
    config: GraphConfig | None = None,
    llm_config: LLMConfig | None = None,
) -> StepStream:
    """Run the agent step by step for a progress view: iterate to get (node, summary)
    pairs as each node finishes, then read `.answer` from the returned stream."""
    graph = build_graph(toolbox, llm, config, llm_config)
    return StepStream(graph, _inputs(question), run_config(config))
