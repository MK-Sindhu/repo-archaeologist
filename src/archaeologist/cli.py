"""Command line interface (`arch`): index a clone, ask why its code is the way it is, mine a
benchmark from its merged PRs, and evaluate the agent against baselines.

    arch index <repo>
    arch ask <repo> "why ...?" [--no-verify] [--no-hops] [--offline] [--json] [--steps]
    arch mine <repo> -n 50 -o bench.jsonl
    arch eval <repo> bench.jsonl [--systems agent,single_commit,plain_rag] [--limit N]

The LLM comes from `make_llm()`, which tests replace with a FakeLLM. Status and progress go
to stderr, so `arch ask --json` prints only the Answer JSON on stdout.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import anthropic
import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from .config import GraphConfig, LLMConfig, llm_provider, load_env_file
from .contracts import LLM
from .eval.baselines import PlainRAGBaseline, SingleCommitBaseline
from .eval.mine import load_benchmark, mine_benchmark, read_jsonl
from .eval.run import run_eval
from .gemini import GeminiLLM, gemini_config
from .graph.graph import ask as run_agent
from .graph.graph import iter_steps
from .index.store import EmbedderMismatchError
from .llm import ClaudeLLM, LLMAuthError, LLMError
from .models import Answer, BenchmarkItem
from .sources.git import GitRepo
from .sources.github import GitHubError
from .toolbox import (
    DEFAULT_MAX_COMMITS,
    RepoToolbox,
    open_github,
    open_index,
    open_toolbox,
    refresh_index,
)

app = typer.Typer(
    name="arch",
    help="Repo Archaeologist: answers 'why is this code like this?' from git and GitHub history.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()
err = Console(stderr=True)

SYSTEMS = ("agent", "agent_no_hops", "agent_no_verify", "single_commit", "plain_rag")
AUTH_HINT = (
    "Claude authentication failed. Set ANTHROPIC_API_KEY to an Anthropic API key in the\n"
    "project's .env file (ANTHROPIC_API_KEY=sk-ant-...) or export it in your shell."
)

RepoArg = Annotated[
    Path, typer.Argument(help="Path to a local git clone (any directory inside it).")
]
OfflineOpt = Annotated[
    bool, typer.Option("--offline", help="Do not call GitHub (local git history and index only).")
]


# -- LLM ----------------------------------------------------------------------------------------


def make_llm() -> LLM:
    """The LLM used by ask / mine / eval: ClaudeLLM or GeminiLLM per `llm_provider()`
    (ARCH_PROVIDER, ARCH_MODEL's prefix, or whichever API key is set). Tests monkeypatch
    this to return a FakeLLM."""
    try:
        provider = llm_provider()
    except ValueError as exc:
        _fail(str(exc), 2)
    llm: LLM = GeminiLLM(gemini_config()) if provider == "gemini" else ClaudeLLM(LLMConfig())
    cfg = _llm_config(llm)
    if cfg is not None:
        grader = f", grading with {cfg.grader_model}" if cfg.grader_model != cfg.model else ""
        err.print(f"[dim]model: {cfg.model}{grader}[/]")
    return llm


def _llm_config(llm: LLM) -> LLMConfig | None:
    config = getattr(llm, "config", None)
    return config if isinstance(config, LLMConfig) else None


class _AuthFailure(BaseException):
    """Raised from inside eval systems so run_eval (which records ordinary exceptions as failed
    rows) stops at the first authentication failure instead of failing every row."""


def _auth_hint(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, LLMAuthError) else AUTH_HINT


def _is_auth_error(exc: BaseException) -> bool:
    if isinstance(exc, (anthropic.AuthenticationError, LLMAuthError)):
        return True
    credentials_error = getattr(anthropic, "CredentialsError", None)
    if isinstance(credentials_error, type) and isinstance(exc, credentials_error):
        return True
    # The SDK raises TypeError before sending when it finds no API key or other credentials.
    return isinstance(exc, TypeError) and "authentication" in str(exc).lower()


def _fail(message: str, code: int = 1) -> NoReturn:
    err.print(f"[bold red]error:[/] {escape(message)}")
    raise typer.Exit(code)


@contextmanager
def _llm_errors() -> Iterator[None]:
    """Turn Claude authentication and API failures into friendly messages."""
    try:
        yield
    except _AuthFailure as exc:
        err.print(f"[bold red]{escape(str(exc) or AUTH_HINT)}[/]")
        raise typer.Exit(2) from None
    except anthropic.APIError as exc:
        if _is_auth_error(exc):
            err.print(f"[bold red]{AUTH_HINT}[/]")
            raise typer.Exit(2) from None
        _fail(f"Claude API error: {exc}")
    except LLMAuthError as exc:
        err.print(f"[bold red]{escape(str(exc))}[/]")
        raise typer.Exit(2) from None
    except LLMError as exc:
        _fail(f"LLM call failed: {exc}")
    except Exception as exc:
        if _is_auth_error(exc):
            err.print(f"[bold red]{escape(_auth_hint(exc))}[/]")
            raise typer.Exit(2) from None
        raise


def _guard_auth(fn: Callable[[str], Answer]) -> Callable[[str], Answer]:
    def run(question: str) -> Answer:
        try:
            return fn(question)
        except Exception as exc:
            if _is_auth_error(exc):
                raise _AuthFailure(_auth_hint(exc)) from exc
            raise

    return run


# -- repo / toolbox -----------------------------------------------------------------------------


@contextmanager
def _progress(label: str) -> Iterator[Callable[[str, int, int | None], None]]:
    with err.status(label) as status:

        def update(stage: str, done: int, total: int | None) -> None:
            status.update(f"{label}: {stage} {done}/{total if total is not None else '?'}")

        yield update


def _git_repo(path: Path) -> GitRepo:
    try:
        return GitRepo(path)
    except (ValueError, RuntimeError) as exc:
        _fail(str(exc), 2)


def _open_toolbox(repo: Path, offline: bool, *, github_max_wait_s: float) -> RepoToolbox:
    try:
        with _progress("Indexing") as progress:
            toolbox = open_toolbox(
                repo, offline=offline, progress=progress, github_max_wait_s=github_max_wait_s
            )
    except EmbedderMismatchError as exc:
        _fail(f"{exc}\nRe-embed it with: arch index {repo} --rebuild", 2)
    except (ValueError, RuntimeError) as exc:
        _fail(str(exc), 2)
    stats = toolbox.build_stats
    if stats and not stats["skipped"]:
        err.print(
            f"[dim]index refreshed: {stats['code']} code, {stats['doc']} doc, "
            f"{stats['commit']} commit chunks[/]"
        )
    if toolbox.github is None and not offline:
        err.print("[yellow]No GitHub remote found: PRs and issues are unavailable.[/]")
    return toolbox


# -- app ----------------------------------------------------------------------------------------


@app.callback()
def main(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Log progress details.")] = False,
) -> None:
    """Repo Archaeologist: follows code -> commit -> PR -> issue and cites every claim."""
    load_env_file()
    logger = logging.getLogger("archaeologist")
    for handler in list(logger.handlers):
        if getattr(handler, "_arch_cli", False):
            logger.removeHandler(handler)
    handler = RichHandler(console=err, show_time=False, show_path=False)
    handler._arch_cli = True  # type: ignore[attr-defined]
    handler.setLevel(logging.INFO if verbose else logging.WARNING)
    logger.addHandler(handler)
    if verbose:
        logger.setLevel(logging.INFO)


@app.command("index")
def index_cmd(
    repo: RepoArg,
    force: Annotated[bool, typer.Option("--force", help="Re-chunk every file.")] = False,
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Re-embed everything (after changing the embedding model)."),
    ] = False,
    max_commits: Annotated[
        int | None,
        typer.Option(
            "--max-commits",
            min=0,
            help=f"Commit messages to index (newest first). Default: previous value or "
            f"{DEFAULT_MAX_COMMITS}.",
        ),
    ] = None,
) -> None:
    """Build or refresh the local search index (code, docs and commit messages)."""
    git = _git_repo(repo)
    try:
        index = open_index(git.root, on_embedder_change="rebuild" if rebuild else "error")
    except EmbedderMismatchError as exc:
        _fail(f"{exc}\nRe-embed it with: arch index {repo} --rebuild", 2)
    with index, _progress("Indexing") as progress:
        stats = refresh_index(
            git, index, max_commits=max_commits, force=force or rebuild, progress=progress
        )
        path = index.db_path
    table = Table(title=f"Index for {git.root.name}", show_header=True)
    table.add_column("chunks")
    table.add_column("total", justify="right")
    table.add_column("added", justify="right")
    for kind in ("code", "doc", "commit"):
        table.add_row(kind, str(stats[kind]), str(stats["added"][kind]))
    console.print(table)
    files = stats["files"]
    skipped = sum(v for k, v in files.items() if k not in ("indexed", "unchanged"))
    head = (stats["head_sha"] or "none")[:12]
    if stats["skipped"]:
        console.print(f"Up to date at HEAD {head}.")
    else:
        console.print(
            f"HEAD {head}: {files['indexed']} files indexed, {files['unchanged']} unchanged, "
            f"{skipped} skipped; {stats['commits']} commits; {stats['removed']} stale chunks "
            "removed."
        )
    console.print(f"[dim]{path}[/]")


@app.command("ask")
def ask_cmd(
    repo: RepoArg,
    question: Annotated[str, typer.Argument(help="The question, e.g. 'why is backoff capped?'")],
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Skip the claim verification step.")
    ] = False,
    no_hops: Annotated[
        bool, typer.Option("--no-hops", help="Do not follow links (commit -> PR -> issue).")
    ] = False,
    offline: OfflineOpt = False,
    json_out: Annotated[bool, typer.Option("--json", help="Print the Answer as JSON.")] = False,
    steps: Annotated[
        bool, typer.Option("--steps", help="Show each agent step as it finishes.")
    ] = False,
) -> None:
    """Answer a question about why the code is the way it is, citing commits, PRs and issues."""
    if not question.strip():
        _fail("the question is empty", 2)
    config = GraphConfig(multi_hop=not no_hops, verify=not no_verify)
    toolbox = _open_toolbox(repo, offline, github_max_wait_s=10.0)
    llm = make_llm()
    llm_config = _llm_config(llm)
    with toolbox, _llm_errors():
        if steps:
            stream = iter_steps(question, toolbox, llm, config, llm_config)
            for node, summary in stream:
                err.print(f"[bold cyan]{node:>13}[/] {escape(summary)}")
            answer = stream.answer
        else:
            with err.status("Digging through the history..."):
                answer = run_agent(question, toolbox, llm, config, llm_config)
    if answer is None:
        _fail("the agent finished without an answer")
    if json_out:
        typer.echo(answer.model_dump_json(indent=2))
    else:
        _print_answer(answer)


def _print_answer(answer: Answer) -> None:
    console.print(Markdown(answer.answer))
    by_id = {e.id: e for e in answer.evidence}
    if answer.citations:
        table = Table(title="Sources", show_header=True, title_justify="left")
        table.add_column("id", style="cyan", overflow="fold")
        table.add_column("title", overflow="fold")
        table.add_column("link", overflow="fold", style="dim")
        for cid in answer.citations:
            item = by_id.get(cid)
            table.add_row(cid, item.title if item else "", (item.url or "") if item else "")
        console.print(table)
    if answer.unsupported_claims:
        console.print("[yellow]Claims the evidence did not support:[/]")
        for claim in answer.unsupported_claims:
            console.print(f"[yellow]  - {escape(claim)}[/]")
    usage = answer.usage
    tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    parts = [
        f"{answer.hops} hop{'s' if answer.hops != 1 else ''}",
        f"{len(answer.evidence)} evidence items",
        f"{usage.get('calls', 0)} LLM calls",
        f"{tokens:,} tokens",
    ]
    if answer.latency_s is not None:
        parts.append(f"{answer.latency_s:.1f} s")
    console.print(f"[dim]{' · '.join(parts)}[/]")


@app.command("mine")
def mine_cmd(
    repo: RepoArg,
    n: Annotated[int, typer.Option("-n", "--n", min=1, help="Target number of items.")] = 50,
    out: Annotated[Path, typer.Option("-o", "--out", help="Benchmark JSONL file.")] = Path(
        "bench.jsonl"
    ),
    max_prs: Annotated[
        int, typer.Option("--max-prs", min=1, help="Merged PRs to scan, newest first.")
    ] = 200,
    min_body_chars: Annotated[
        int, typer.Option("--min-body-chars", min=0, help="Skip PRs with shorter descriptions.")
    ] = 200,
) -> None:
    """Mine benchmark questions from the repository's merged pull requests (needs GitHub)."""
    git = _git_repo(repo)
    gh = open_github(git, max_wait_s=60.0)
    if gh is None:
        _fail("mining needs a GitHub remote (e.g. origin = https://github.com/<owner>/<repo>)", 2)
    if not gh.authenticated:
        err.print(
            "[yellow]No GitHub token (GITHUB_TOKEN, GH_TOKEN or `gh auth login`): "
            "unauthenticated requests are limited to 60 per hour.[/]"
        )
    llm = make_llm()
    slug = f"{gh.owner}/{gh.repo}"
    with gh, _llm_errors():
        try:
            with err.status(f"Mining {slug}..."):
                items = mine_benchmark(
                    gh,
                    git,
                    llm,
                    n,
                    out,
                    gh.owner,
                    gh.repo,
                    min_body_chars,
                    max_prs=max_prs,
                )
        except GitHubError as exc:
            _fail(f"GitHub request failed: {exc}")
    total = sum(1 for row in read_jsonl(out) if row.get("repo") == slug)
    console.print(f"Mined {len(items)} new item(s) into {out} ({total} for {slug}).")
    for item in items:
        console.print(f"  [cyan]{item.id}[/] {escape(item.question)}")
    if total < n:
        console.print(
            f"[yellow]Fewer than {n} items: raise --max-prs or lower --min-body-chars; run with "
            "-v to see why PRs were skipped.[/]"
        )


def _systems(
    names: list[str], toolbox: RepoToolbox, llm: LLM, llm_config: LLMConfig | None
) -> dict[str, Callable[[str], Answer]]:
    def agent(name: str, config: GraphConfig) -> Callable[[str], Answer]:
        def run(question: str) -> Answer:
            answer = run_agent(question, toolbox, llm, config, llm_config)
            return answer.model_copy(update={"system": name})

        return run

    factories: dict[str, Callable[[], Callable[[str], Answer]]] = {
        "agent": lambda: agent("agent", GraphConfig()),
        "agent_no_hops": lambda: agent("agent_no_hops", GraphConfig(multi_hop=False)),
        "agent_no_verify": lambda: agent("agent_no_verify", GraphConfig(verify=False)),
        "single_commit": lambda: SingleCommitBaseline(toolbox, llm),
        "plain_rag": lambda: PlainRAGBaseline(toolbox, llm),
    }
    return {name: _guard_auth(factories[name]()) for name in names}


def _parse_systems(value: str) -> list[str]:
    names = list(dict.fromkeys(s.strip() for s in value.split(",") if s.strip()))
    unknown = [s for s in names if s not in SYSTEMS]
    if unknown or not names:
        raise typer.BadParameter(
            f"unknown system(s) {', '.join(unknown) or '(none given)'}; "
            f"choose from {', '.join(SYSTEMS)}",
            param_hint="--systems",
        )
    return names


def _select_items(
    items: list[BenchmarkItem], slug: str | None, limit: int | None
) -> list[BenchmarkItem]:
    if slug is not None:
        mine = [i for i in items if i.repo.lower() == slug.lower()]
        if mine and len(mine) < len(items):
            err.print(f"[dim]skipping {len(items) - len(mine)} item(s) from other repositories[/]")
        if mine:
            items = mine
        elif items:
            err.print(f"[yellow]No items for {slug} in the benchmark; running all of them.[/]")
    return items[:limit] if limit is not None else items


@app.command("eval")
def eval_cmd(
    repo: RepoArg,
    bench: Annotated[Path, typer.Argument(help="Benchmark JSONL written by `arch mine`.")],
    systems: Annotated[
        str,
        typer.Option("--systems", help=f"Comma-separated systems from: {', '.join(SYSTEMS)}."),
    ] = ",".join(SYSTEMS),
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Evaluate only the first N items.")
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Output directory (results, summary, report).")
    ] = Path("results/run"),
    offline: OfflineOpt = False,
) -> None:
    """Run the agent, its ablations and the baselines on a benchmark; judge and report."""
    names = _parse_systems(systems)
    if not bench.is_file():
        _fail(f"benchmark file not found: {bench}", 2)
    try:
        items = load_benchmark(bench)
    except ValueError as exc:
        _fail(f"invalid benchmark file {bench}: {exc}", 2)
    toolbox = _open_toolbox(repo, offline, github_max_wait_s=60.0)
    slug = f"{toolbox.owner}/{toolbox.name}" if toolbox.owner and toolbox.name else None
    items = _select_items(items, slug, limit)
    if not items:
        toolbox.close()
        _fail(f"no benchmark items in {bench}", 2)
    llm = make_llm()
    llm_config = _llm_config(llm)
    model = llm_config.model if llm_config else LLMConfig().model
    total = len(items) * len(names)
    done = 0

    def on_row(row: dict[str, Any]) -> None:
        nonlocal done
        done += 1
        if row.get("error"):
            status = f"[red]error[/] {escape(str(row['error']))[:120]}"
        else:
            cited = "yes" if row.get("gold_hit_cited") else "no"
            status = f"judge {row.get('judge_score')} · gold cited {cited}"
        err.print(f"[dim]{done}/{total}[/] {row['item_id']} [cyan]{row['system']}[/] {status}")

    with toolbox, _llm_errors():
        summary = run_eval(
            items,
            _systems(names, toolbox, llm, llm_config),
            llm,
            out,
            model_for_cost=model,
            on_row=on_row,
            system_llm=llm,
        )
    console.print(_summary_table(summary))
    console.print(f"[dim]full report: {out / 'report.md'} (rows: {out / 'results.jsonl'})[/]")


def _summary_table(summary: dict[str, Any]) -> Table:
    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value * 100:.0f}%"

    def num(value: float | None, spec: str) -> str:
        return "n/a" if value is None else format(value, spec)

    n = summary["n_items"]
    table = Table(title=f"{n} item{'' if n == 1 else 's'}", title_justify="left")
    table.add_column("system", style="cyan", no_wrap=True)
    for name in ("n", "err", "gold cited", "gold found", "judge", "valid cites", "hops", "cost"):
        table.add_column(name, justify="right")
    for name, s in summary["systems"].items():
        cost = s["cost_usd"]
        table.add_row(
            name,
            str(s["n"]),
            str(s["errors"]),
            pct(s["gold_cited"]),
            pct(s["gold_retrieved"]),
            num(s["judge_mean"], ".2f"),
            pct(s["citation_validity"]),
            num(s["hops_mean"], ".1f"),
            "n/a" if cost is None else f"${cost:,.2f}",
        )
    return table


if __name__ == "__main__":  # pragma: no cover
    app()
