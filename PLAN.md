# Repo Archaeologist — Plan

**Goal:** an open-source LangGraph agent that answers *"why is this code like this?"* by following
code → `git blame`/`git log -L` → commit → PR → linked issue, citing every claim — plus a
**public benchmark** showing how much each feature helps over simpler systems.

**Why it's different:** Unblocked/Qodo are closed with no published accuracy; DeepWiki mostly
documents current code; git-why/semblame look at one commit. The Argus paper (arXiv 2604.10345)
shows rationale is split across commits/PRs (goal) and issues (need) — so multi-hop matters.

---

## Architecture

```
            ┌──────────── contracts.py (Toolbox, LLM, Embedder protocols) ────────────┐
            │                                                                         │
 sources/git.py ──┐                                                                   │
 sources/github.py┼──► toolbox.py (RepoToolbox: search / history / fetch) ──► graph/ ─┼─► cli.py
 index/*  ────────┘                                                         eval/ ────┘
 llm.py (ClaudeLLM) ───────────────────────────────────────────────────────► graph/, eval/
```

Core (done, owned by the lead — **do not modify** without flagging it):
`config.py`, `models.py` (Evidence, Chunk, Hit, Answer, BenchmarkItem), `contracts.py`,
`refs.py` (canonical refs + `extract_refs`), `llm.py` (ClaudeLLM), `testing.py`
(FakeLLM, FakeToolbox, HashEmbedder, `ev()`), `tests/conftest.py` (`toy_repo` fixture).

Canonical refs (refs.py): `commit:<sha>`, `pr:<n>`, `issue:<n>`, `gh:<n>` (PR-or-issue),
`code:<path>#L<a>-L<b>`, `doc:<path>#<slug>`. Evidence ids and citations use these.

---

## Modules (each owned by one builder)

### A. `sources/git.py` — local git (subprocess, no GitPython)
`GitRepo(path)` with:
- `head_sha()`, `ls_files() -> list[str]`, `read_file(path, rev="HEAD") -> str`
- `remote_slug() -> tuple[str, str] | None` — parse origin (https/ssh) → (owner, repo)
- `iter_commits(max_count=None, paths=None) -> Iterator[CommitInfo]` (newest first)
- `show(sha, max_diff_chars=6000) -> CommitInfo` (with diff)
- `blame(path, start, end, rev="HEAD") -> list[BlameHunk]` (porcelain)
- `log_lines(path, start, end, max_count=10)` and `log_symbol(path, symbol, max_count=10)`
  → `list[CommitInfo]` via `git log -L` (funcname form for symbol, falls back to line range)
- `pickaxe(term, max_count=10)` → `git log -S`
- `CommitInfo`: sha, short_sha, author, date (ISO), subject, body, files, diff | None;
  `.to_evidence(owner=None, repo=None) -> Evidence` (kind="commit", id=`commit:<sha>`,
  refs from `extract_refs(subject+body)`, url when owner/repo known)
- `BlameHunk`: sha, start, end, author, date, summary
- Robust to: binary files, renames, missing paths, empty repos, huge diffs (truncate).

### B. `sources/github.py` — GitHub REST (httpx) with SQLite cache
`GitHubClient(owner, repo, token=None, cache_path=None, transport=None)`:
- token: arg → `GITHUB_TOKEN`/`GH_TOKEN` → `gh auth token` → None (unauthenticated)
- `prs_for_commit(sha) -> list[PullRequest]` (`/commits/{sha}/pulls`)
- `get_pr(n, with_comments=True) -> PullRequest | None` (body, issue comments, review comments, digest)
- `get_issue(n) -> Issue | None` (404 → None; if it is actually a PR, `is_pr=True`)
- `resolve(n) -> PullRequest | Issue | None` (for `gh:<n>`)
- `search(query, k=10, kind="both")` (`/search/issues`, `repo:` qualifier)
- `list_merged_prs(limit=200, base=None) -> list[PullRequest]` (for eval mining)
- `pr_files(n) -> list[PRFile]` (filename, status, patch)
- `PullRequest.to_evidence()` / `Issue.to_evidence()`: refs = closing-keyword issues + `#n`
  mentions + merge commit; text = body + comments digest (truncated).
- Cache every GET in SQLite (url → json, etag); serve from cache; honour 304.
- Rate limits: on 403/429 with reset header → wait (capped) or raise `RateLimited`; never crash
  the agent — callers decide. All tests use `httpx.MockTransport`.

### C. `index/` — chunking + hybrid search
- `index/chunkers.py`: `chunk_python(path, source)` (ast: functions, classes, methods; qualified
  symbol names; line ranges; class chunks summarise members) · `chunk_text(path, source, window=60,
  overlap=10)` fallback for other languages · `chunk_markdown(path, source)` (by headings,
  id `doc:<path>#<slug>`) · `commit_chunk(CommitInfo)` (subject + body + file list).
- `index/embed.py`: `FastEmbedEmbedder(model="BAAI/bge-small-en-v1.5")` (lazy import) and
  `get_default_embedder()` → FastEmbed if importable else None (BM25-only).
- `index/store.py`: `HybridIndex(db_path, embedder=None)` — SQLite tables for chunks, FTS5
  (bm25) with a code-aware tokenizer step (split snake_case/CamelCase into extra tokens),
  embeddings as float32 BLOBs; `add(chunks)`, `search(query, k=8, kinds=None) -> list[Hit]`
  fused with Reciprocal Rank Fusion (k=60); `get(id)`, `count(kind=None)`, `clear()`.
  Upsert by id. Safe FTS query escaping.
- `index/build.py`: `build_index(repo: GitRepo, index, max_commits=5000, max_file_bytes=200_000,
  include=(…exts…), exclude=(vendored dirs, lockfiles, minified))` → stats dict. Incremental:
  skip if `meta.head_sha` unchanged.

### D. `graph/` — the LangGraph agent
`build_graph(toolbox: Toolbox, llm: LLM, config: GraphConfig) -> CompiledStateGraph` and
`ask(question, toolbox, llm, config) -> Answer`.

```
analyze ──► (Send ×N) retrieve[source] ──► trace_history ──► grade ──┐
                                                                     ▼
              ┌────────────── follow (fetch refs) ◄── route: frontier & hops < max
              ▼                                      │
            grade ─────────────────────────────────► synthesize ──► verify ──► finalize
                                                        ▲              │
                                                        └── retry ◄────┘ (unsupported & retries left)
```
- **analyze** (LLM, structured `Analysis`): question type, code targets (paths/symbols), one
  query per enabled source.
- **retrieve** (fan-out with `Send`, one per source in `config.sources` minus "history").
- **trace_history**: for top code hits / named targets → `toolbox.history(...)`.
- **grade** (LLM role="grade", effort low, structured `Grades`): relevance 0–3 per *new* item,
  keep score ≥ 2, cap `max_evidence`; choose refs to follow from kept items' `refs`.
- **follow**: `toolbox.fetch(ref)` for ≤ `max_follow_per_hop` unvisited refs; hops += 1.
- **synthesize** (structured `Draft`): answer citing `[evidence-id]` inline; only ids in state.
- **verify** (structured `Verification`): per claim supported? → unsupported list + follow-up
  queries. Retry once via retrieve→grade→synthesize, else finalize dropping/flagging claims.
- **finalize** → `Answer` (citations = ids actually cited & present; trace; usage; hops).
- Ablations via `GraphConfig`: `multi_hop=False` (no follow), `verify=False`, `sources=[...]`.
- State uses reducers (evidence merged by id; trace appended). No network in tests: FakeLLM +
  FakeToolbox only.

### E. `eval/` — benchmark + baselines + metrics
- `eval/mine.py`: `mine_benchmark(gh, repo, llm, n, out_path)` — merged PRs with substantive
  bodies (≥ 200 chars, not bots, not docs/CI-only), merge commit present locally; pick one changed
  function from the patch; LLM writes a question **from the diff only**; reference rationale =
  PR body + linked issues (LLM-condensed). **Leakage guard:** reject questions sharing ≥ 5-word
  n-grams with the PR title/body or containing the PR number. Writes JSONL of `BenchmarkItem`.
- `eval/baselines.py`: `SingleCommitBaseline` (git-why style: locate code via index → blame →
  newest commit → explain) and `PlainRAGBaseline` (one hybrid search over code+commits+docs →
  answer). Both → `Answer`. They take a Toolbox + LLM (same interfaces as the agent).
- `eval/judge.py`: `judge(item, answer, llm) -> Judgement` (score 0–2 vs reference rationale +
  reason; never sees gold ids).
- `eval/metrics.py`: `gold_hit_cited`, `gold_hit_retrieved` (same_ref against gold PR / commits /
  issues), `citation_validity` (cited ids ⊆ evidence ids), aggregate means, cost.
- `eval/run.py`: `run_eval(items, systems: dict[str, Callable[[str], Answer]], llm_judge, out_dir)`
  → per-item JSONL + `summary.json` + `report.md` (markdown table; ablation rows).
- **Leakage at run time:** for each item, toolbox must hide PRs/issues created after the gold PR's
  merge? No — the gold PR *is* the answer we want found. What we hide is the *question's* source
  text; the systems must find the PR themselves. (DeepWiki comparison: phase 2.)

### F. Integration (after A–E)
- `toolbox.py`: `RepoToolbox(repo: GitRepo, index: HybridIndex, github: GitHubClient | None)`
  implementing `contracts.Toolbox`; `open_toolbox(repo_path, offline=False) -> RepoToolbox`.
- `cli.py` (typer, entry `arch`): `arch index <repo>`, `arch ask <repo> "<q>" [--no-verify]
  [--no-hops] [--json]`, `arch mine <repo> -n 50 -o bench.jsonl`, `arch eval <repo> bench.jsonl
  [--systems agent,single_commit,plain_rag,agent_no_hops,agent_no_verify]`.
- End-to-end test on `toy_repo` with FakeLLM + mocked GitHub. README quickstart.

---

## Milestones
1. **M1 (this build):** all modules + offline tests green; `arch ask` works end-to-end with a key.
2. **M2:** mine 50–100 items from 2–3 real repos (e.g. `encode/httpx`, `pallets/click`,
   `langchain-ai/langgraph`); run agent vs baselines + ablations; publish `report.md` + chart.
3. **M3:** DeepWiki MCP baseline; Argus data import; streaming UI showing graph steps (LangGraph
   `stream_mode="updates"`); LangSmith tracing.

## Risks
- **Cost:** every eval run calls Claude; mining + judging too. Start with n=20; log usage.
- **GitHub rate limits:** use `gh auth token`; cache aggressively; fetch lazily.
- **Leakage:** question generator sees only the diff; n-gram guard; judge never sees gold ids.
- **Big repos:** index caps (file size, commit count), incremental rebuilds.

## Conventions
- Python 3.12, `uv run pytest`, ruff line length 100. No network in default tests
  (`@pytest.mark.live` for anything that needs it).
- Don't edit `pyproject.toml`/`uv.lock` in parallel builds — request deps from the lead.
- Commits: no Claude co-author trailers.
