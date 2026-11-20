# Repo Archaeologist

An open-source LangGraph agent that answers *"why is this code like this?"* about any git
repository. It locates the code, traces its history with `git log -L`, and follows the
links from each commit to its pull request and the issues behind it
(code → commit → PR → issue). Every claim in the answer cites the commit, PR, issue, code
or doc it rests on. A benchmark mined from real merged PRs measures how much each step
helps compared with simpler systems.

**How it differs.** Commercial code-understanding assistants such as Unblocked and Qodo are
closed and publish no accuracy numbers. DeepWiki mostly documents the code as it is now.
Blame-style tools such as git-why and semblame explain from a single commit. The Argus
paper (arXiv 2604.10345) found that a change's rationale is usually split across commits
and PRs (the goal) and issues (the need). Repo Archaeologist follows those links over
several hops, checks each claim against the evidence, and reports benchmark results for
the full agent, its ablations and the simpler baselines.

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and git.

The quickest way in is the launcher: it installs dependencies, creates `.env` on first run, clones
GitHub repos when given `owner/name` or a URL, indexes, and then answers questions:

```bash
./start.sh pallets/click                                  # then type questions interactively
./start.sh path/to/repo "why does backoff() cap the delay?"   # one question
```

Or step by step:

```bash
uv sync --extra embed               # the embed extra adds FastEmbed for dense search (optional)
cp .env.example .env                # then put ANTHROPIC_API_KEY or GEMINI_API_KEY in .env

uv run arch index path/to/repo
uv run arch ask path/to/repo "why does backoff() cap the delay at 30 seconds?"
```

`arch ask` options:

| flag | effect |
|---|---|
| `--steps` | print each agent step (analyze, retrieve, trace_history, grade, follow, ...) as it finishes |
| `--json` | print the full `Answer` (answer, citations, evidence, trace, usage) as JSON on stdout |
| `--offline` | use local git history and the index only; PRs and issues are skipped |
| `--no-hops` | do not follow links (commit → PR → issue) |
| `--no-verify` | skip the claim-verification step |

`arch ask` refreshes the index first. The refresh is incremental and does nothing when HEAD
has not moved. GitHub is used when the clone's remote (origin first) points at github.com.
Without a token, GitHub allows only 60 requests an hour. Responses are cached, and once a
rate limit is hit the agent stops making calls charged to that limit (search, or the rest of
the API) until it resets.

Build a benchmark from a repository's merged PRs and evaluate on it:

```bash
uv run arch mine path/to/repo -n 50 -o bench.jsonl
uv run arch eval path/to/repo bench.jsonl --systems agent,single_commit,plain_rag --limit 20 --out results/run
```

`--systems` takes any of `agent`, `agent_no_hops`, `agent_no_verify`, `single_commit` and
`plain_rag`; the default runs all five. Every eval run calls Claude for each item, system
and judgement, so start small with `--limit`.

### Configuration

Set these in your shell or in a `.env` file (git-ignored), read from the current directory and
the project checkout. Shell variables win over `.env`; `ARCH_NO_DOTENV=1` turns the file off.

**Two LLM backends.** Claude (`claude-opus-5-5`, the default) or Gemini, which has a free tier
([get a key](https://aistudio.google.com/apikey)). With only a Gemini key set, Gemini is used
automatically: `gemini-3.5-flash` for every step, `gemini-3.5-flash-lite` for grading. On the free
tier expect ~20–40 s per call (a question takes a few minutes), retries on 429/503, and a daily
request cap per model. The published benchmark numbers use Claude.

| variable | default | meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none) | Claude API key |
| `GEMINI_API_KEY` (or `GOOGLE_API_KEY` / `GOOGLE_GEMINI_KEY`) | (none) | Gemini API key |
| `ARCH_PROVIDER` | from `ARCH_MODEL`, else whichever key is set (Claude first) | `anthropic` or `gemini` |
| `ARCH_MODEL` / `ARCH_GRADER_MODEL` | `claude-opus-5-5` (Gemini: `gemini-3.5-flash` / `-flash-lite`) | model for every step, or for relevance grading only |
| `ARCH_GEMINI_CONCURRENCY` / `ARCH_GEMINI_RPM` | `2` / off | Gemini: max in-flight calls / requests-per-minute pacing |
| `ARCH_USE_FALLBACKS` | `1` | server-side refusal fallback beta; set `0` if your account rejects it |
| `GITHUB_TOKEN` / `GH_TOKEN` | `gh auth token` | GitHub token |
| `ARCH_EMBEDDER` | FastEmbed if installed | `none` gives BM25-only search |
| `ARCH_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | FastEmbed model (downloaded on first use) |
| `ARCH_HOME` | `~/.cache/repo-archaeologist` | where indexes and GitHub caches live, one directory per clone |

Nothing is written inside the target repository. If you change the embedding model, re-embed
with `arch index <repo> --rebuild`. `arch index --max-commits N` caps how many commit
messages are indexed, and later refreshes keep that cap.

## Architecture

```
            ┌──────────── contracts.py (Toolbox, LLM, Embedder protocols) ────────────┐
            │                                                                         │
 sources/git.py ──┐                                                                   │
 sources/github.py┼──► toolbox.py (RepoToolbox: search / history / fetch) ──► graph/ ─┼─► cli.py
 index/*  ────────┘                                                         eval/ ────┘
 llm.py (ClaudeLLM) ───────────────────────────────────────────────────────► graph/, eval/
```

- `sources/git.py`: local git through the git CLI: blame, `git log -L` for line ranges
  and symbols (Python symbols are located with `ast`, and commits that never changed them
  are dropped), pickaxe, and `show` with truncated diffs.
- `sources/github.py`: GitHub REST client (httpx) with a SQLite ETag cache. It fetches PRs
  with their review and conversation comments, issues, commit → PR associations and search
  results, and handles rate limits.
- `index/`: chunking (Python `ast` symbols, markdown/rst sections, line windows for other
  languages, commit messages) and a hybrid index: SQLite FTS5 BM25 with snake_case and
  CamelCase splitting, plus optional FastEmbed vectors, fused with reciprocal rank fusion.
  Rebuilds are incremental.
- `toolbox.py`: `RepoToolbox` implements the `Toolbox` protocol the agent uses:
  `search(code|docs|commits|prs)`, `history(path, lines | symbol)` and `fetch(ref)` for the
  canonical refs `commit:<sha>`, `pr:<n>`, `issue:<n>`, `gh:<n>`, `code:<path>#L<a>-L<b>`
  and `doc:<path>#<slug>`. Commit evidence always names its PR, from the message or from
  GitHub. GitHub failures degrade to empty results and never crash the agent.
- `graph/`: the agent:

```
analyze ──► (Send ×N) retrieve[source] ──► trace_history ──► grade ──┐
                                                                     ▼
              ┌────────────── follow (fetch refs) ◄── route: frontier & hops < max
              ▼                                      │
            grade ─────────────────────────────────► synthesize ──► verify ──► finalize
                                                        ▲              │
                                                        └── retry ◄────┘ (unsupported & retries left)
```

  1. `analyze` plans one query per source and names code targets.
  2. `retrieve` fans out in parallel, one task per source.
  3. `trace_history` takes `git log -L` for the targets and the top code hits.
  4. `grade` scores new evidence 0-3 and keeps items scoring 2 or more; `follow` fetches the
     refs of kept commits, PRs and issues, up to `max_hops` hops.
  5. `synthesize` writes an answer citing `[evidence-id]` inline.
  6. `verify` checks each claim and can trigger one retry with follow-up queries.
  7. `finalize` keeps only citations that point at retrieved evidence.

  Retrieved text is treated as untrusted data in every prompt.
- `eval/`: benchmark mining, the baselines, the LLM judge, metrics and the report.
- `cli.py`: the `arch` command.

## Evaluation protocol

**Mining** (`arch mine`). Candidates are merged PRs with a substantive description (at least
200 characters, not written by a bot, not only docs, tests, CI or config) whose merge commit
exists in the local clone. One changed Python function is picked from each PR's patch.

- **Question.** The LLM writes the question from that function's diff hunks only. It never
  sees the PR title, body or number.
- **Reference rationale.** A separate LLM call condenses the PR description, its discussion
  and its linked issues into 2-5 sentences.
- **Leakage guard.** A question is rejected if it names an issue or PR reference (`#n`,
  `GH-n`, a GitHub link), a commit SHA, the PR number or a linked issue's number, or shares any
  5-word n-gram with the PR title or body.
- **Output.** Each item records the gold PR, its merge commit (plus the branch commits a
  true merge commit brought in) and the issues the PR title or body references, and is
  written to JSONL. A PR that fails (an LLM refusal, a GitHub error) is skipped. Re-running
  tops the file up to `-n` items for that repository.

**Systems.**

| system | what it does |
|---|---|
| `agent` | the full graph |
| `agent_no_hops` | no link following (`multi_hop=False`) |
| `agent_no_verify` | no verification step |
| `single_commit` | git-why style: best code hit → newest commit that touched it → one answer |
| `plain_rag` | one hybrid search over code, commits and docs → one answer |

Every system gets the same toolbox and model, and sees only the question: never the gold
ids, the PR title or the reference rationale. Finding the gold PR is part of the task.

**Metrics** (per system):

| metric | meaning |
|---|---|
| gold cited | the answer cites the gold PR, its merge commit or a linked issue (abbreviated SHAs and `gh:<n>` count) |
| gold retrieved | the same, but anywhere in what the system retrieved (for the agent, before grading) |
| judge (0-2) | an LLM compares the answer's stated reason with the reference rationale |
| citation validity | share of cited ids that point at evidence the system actually retrieved (invented ids count against it) |
| hops, latency, tokens, estimated cost | from each answer's trace and usage |

The judge never sees gold ids, and refs, SHAs, URLs and `#numbers` are redacted from its
inputs. A system error counts as a miss.

**Output** (`--out results/run`): `results.jsonl` has one row per (item, system). It is
appended to, so an interrupted run resumes, failed pairs are retried, and adding a system
computes only its rows.
`summary.json` holds the aggregates and `report.md` the table.

## Project status

- **M1 (this build): code complete.**
  - Everything above is implemented: git and GitHub sources, the hybrid index, the agent,
    the toolbox, the CLI, mining, baselines, judge and metrics.
  - The offline test suite passes (`uv run pytest`). It uses a real git fixture repo, a
    mocked GitHub API and a scripted fake LLM, with no network and no keys.
  - Tests marked `live` (real GitHub, real FastEmbed) are deselected by default; run them
    with `uv run pytest -m live`.
  - `arch ask` has not yet been checked end to end against the live Claude API.
- **M2 (next):**
  - Mine 50-100 items from 2-3 public repositories, e.g. `encode/httpx`, `pallets/click`
    and `langchain-ai/langgraph`.
  - Run the agent against the baselines and ablations, and publish `report.md` with a chart.
  - No benchmark results exist yet.
- **M3:**
  - A DeepWiki baseline (via MCP) and an import of the Argus data.
  - A streaming UI showing graph steps, and LangSmith tracing.

## Development

```bash
uv sync --extra embed
uv run pytest -q              # offline tests
uv run ruff check src tests
```

See [PLAN.md](PLAN.md) for the design and module contracts.
