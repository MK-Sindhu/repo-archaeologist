"""Local git access through the git CLI (subprocess only, no GitPython).

`GitRepo` answers the history questions the agent asks: what is at HEAD, who last touched these
lines (blame), which commits shaped this range or symbol (`git log -L`), and where a term was
added or removed (pickaxe). Every query method fails soft: missing paths, bad revisions, empty
repositories and git errors give `[]` or `None`, never an exception. Only the constructor raises
(`ValueError`) when the path is not inside a git work tree.

Thread safety: a `GitRepo` holds only immutable state after construction and every call runs
its own git process, so one instance can serve LangGraph's concurrent fan-out nodes.
"""

from __future__ import annotations

import ast
import atexit
import codecs
import difflib
import logging
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import warnings
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel, Field

from ..models import Evidence
from ..refs import commit_ref, extract_refs, same_ref

__all__ = ["BlameHunk", "CommitInfo", "GitRepo", "parse_github_remote"]

log = logging.getLogger(__name__)

_RS = "\x1e"  # record separator: starts every commit in our --format
_FS = "\x1f"  # field separator
# sha, short sha, author (mailmap-aware), strict ISO author date, subject, body
_LOG_FORMAT = "%x1e%H%x1f%h%x1f%aN%x1f%aI%x1f%s%x1f%b%x1f"
_RANGE_FORMAT = "%x1e%H%x1f"  # `git log -L`: metadata is fetched separately, keep only the patch
_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")  # SHA-1 or SHA-256 object ids
_TRUNCATED = "\n… [diff truncated]"
_SHALLOW_NOTE = (
    "Note: this is the oldest commit of a shallow clone. Earlier history is missing, so this "
    "commit shows its files as newly added and may not be where this code was introduced."
)
_EVIDENCE_MAX_FILES = 50
_STREAM_CHUNK = 1 << 16
_BINARY_SNIFF = 8000  # git's own heuristic: a NUL in the first 8000 bytes means binary

# Map extensions to git's builtin funcname drivers so `git log -L :<name>:<file>` and hunk
# headers recognise indented definitions (Python methods, Java methods, ...). Repo-level
# .gitattributes still take precedence over this file.
_DIFF_ATTRIBUTES = """\
*.py diff=python
*.pyi diff=python
*.pyw diff=python
*.go diff=golang
*.rs diff=rust
*.java diff=java
*.kt diff=kotlin
*.kts diff=kotlin
*.rb diff=ruby
*.php diff=php
*.c diff=cpp
*.h diff=cpp
*.cc diff=cpp
*.cpp diff=cpp
*.cxx diff=cpp
*.hpp diff=cpp
*.cs diff=csharp
*.pl diff=perl
*.pm diff=perl
*.sh diff=bash
*.bash diff=bash
*.ex diff=elixir
*.exs diff=elixir
*.css diff=css
*.html diff=html
*.tex diff=tex
*.md diff=markdown
*.markdown diff=markdown
"""

# Per-command config making output parseable regardless of the user's git settings.
_GIT_CONFIG = (
    "core.quotepath=off",
    "color.ui=never",
    "log.showSignature=false",
    "i18n.logOutputEncoding=UTF-8",
    "diff.noprefix=false",
    "diff.mnemonicPrefix=false",
)

# Environment variables that would redirect git away from the repository given by `-C`.
_REPO_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)

_PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")
_SYMBOL_RENAME_RATIO = 0.6  # similarity needed to treat a vanished symbol as renamed
_CHAR_COMPARE_MAX = 4000  # above this many characters (both sources), compare lines instead
_DUP = "#"  # separates a repeated definition's ordinal: "Box.size#2" is the second Box.size
# Old Python sources often contain invalid escape sequences; parsing them under this filename
# lets one narrow warning filter silence the resulting SyntaxWarnings.
_AST_FILENAME = "<archaeologist-git-history>"
_AST_WARNING_MODULE = re.escape(_AST_FILENAME)

_ATTR_LOCK = threading.Lock()
_ATTR_PATH: str | None = None
_VERSION_LOCK = threading.Lock()
_GIT_VERSION: tuple[int, int] | None = None


class CommitInfo(BaseModel):
    """One commit as seen by `git log` / `git show`."""

    sha: str
    short_sha: str
    author: str
    date: str  # ISO-8601 author date
    subject: str
    body: str = ""
    files: list[str] = Field(default_factory=list)  # paths changed (vs first parent for merges)
    diff: str | None = None  # patch text, truncated; None when not requested
    # oldest commit of a shallow clone: its parents are missing, so it shows every file as
    # newly added and looks like the commit that introduced all of the code
    shallow_boundary: bool = False

    def to_evidence(self, owner: str | None = None, repo: str | None = None) -> Evidence:
        """Convert to `kind="commit"` Evidence with refs extracted from the message.

        `owner`/`repo` restrict URL refs to that GitHub repository and enable `url`. A shallow
        clone's boundary commit says so in its text and in meta["shallow_boundary"].
        """
        message = f"{self.subject}\n\n{self.body}".strip()
        parts = [message]
        if self.shallow_boundary:
            parts.append(_SHALLOW_NOTE)
        if self.files:
            shown = self.files[:_EVIDENCE_MAX_FILES]
            more = len(self.files) - len(shown)
            parts.append("Files: " + ", ".join(shown) + (f" (+{more} more)" if more else ""))
        if self.diff:
            parts.append("Diff:\n" + self.diff)
        own = commit_ref(self.sha)
        refs = [r for r in extract_refs(message, owner, repo) if not same_ref(r, own)]
        url = f"https://github.com/{owner}/{repo}/commit/{self.sha}" if owner and repo else None
        meta: dict[str, object] = {"author": self.author, "short_sha": self.short_sha}
        if self.shallow_boundary:
            meta["shallow_boundary"] = True
        return Evidence(
            id=own,
            kind="commit",
            title=self.subject or f"commit {self.short_sha}",
            text="\n\n".join(parts),
            url=url,
            source="git",
            date=self.date,
            refs=refs,
            meta=meta,
        )


class BlameHunk(BaseModel):
    """A run of consecutive lines (1-based, inclusive) last changed by one commit."""

    sha: str
    start: int
    end: int
    author: str
    date: str  # ISO-8601 author date
    summary: str
    shallow_boundary: bool = False  # see CommitInfo.shallow_boundary


class _RangeCommit(NamedTuple):
    """A commit printed by `git log -L`, with its range-restricted patch."""

    sha: str
    diff: str


class _Symbol(NamedTuple):
    start: int  # first line, decorators included
    end: int
    kind: str  # "def" | "class" | "assign"


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """Parse a github.com remote URL into (owner, repo).

    Accepts https/http/git/ssh URLs (optionally with user, port, trailing `.git` or `/`) and
    scp-style `git@github.com:owner/repo.git`. Other hosts give None.
    """
    url = url.strip()
    slug = r"(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+?)(?:\.git)?/?"
    patterns = (
        r"(?:https?|git|ssh|git\+ssh)://(?:[^@/]+@)?(?:www\.|ssh\.)?github\.com(?::\d+)?/" + slug,
        r"(?:[^@/:]+@)?(?:ssh\.)?github\.com:" + slug,  # scp-like: git@github.com:owner/repo
    )
    for pattern in patterns:
        m = re.fullmatch(pattern, url, flags=re.IGNORECASE)
        if m and m["repo"] not in (".", ".."):
            return m["owner"], m["repo"]
    return None


class GitRepo:
    """Read-only view of a local git repository, driven through the git CLI."""

    def __init__(self, path: str | Path, *, timeout: float = 60.0) -> None:
        """Open the work tree containing `path`.

        `timeout` (seconds) bounds every git call except streaming `iter_commits` (unless it
        is given its own `timeout`) and `pickaxe`. Raises ValueError if `path` is not inside a
        git work tree, RuntimeError if git is not installed.
        """
        if shutil.which("git") is None:
            raise RuntimeError("git executable not found on PATH")
        start = Path(path).expanduser()
        if not start.is_dir():
            raise ValueError(f"not a git repository (no such directory): {path}")
        self.timeout = timeout
        self._env = _git_env()
        self._attributes = _attributes_file()
        self.root = start  # provisional: lets _run work for the discovery call below
        top = self._run(["rev-parse", "--show-toplevel"])
        if not top or not top.strip():
            raise ValueError(f"not a git repository: {path}")
        self.root = Path(top.decode("utf-8", "replace").strip())
        self._merge_args: tuple[str, ...] = (
            ("--diff-merges=first-parent",) if _git_version() >= (2, 31) else ()
        )
        self._shallow = self._shallow_boundary()

    def __repr__(self) -> str:
        return f"GitRepo({str(self.root)!r})"

    @property
    def is_shallow(self) -> bool:
        """True for a shallow clone (`git clone --depth N`, CI checkouts): history is cut off
        at boundary commits, marked `shallow_boundary` in CommitInfo / BlameHunk."""
        return bool(self._shallow)

    def _shallow_boundary(self) -> frozenset[str]:
        """Shas listed in the repository's `shallow` file (empty for a full clone)."""
        out = self._run(["rev-parse", "--git-path", "shallow"])
        if not out or not out.strip():
            return frozenset()
        path = Path(out.decode("utf-8", "replace").strip())
        if not path.is_absolute():
            path = self.root / path
        try:
            text = path.read_text(encoding="ascii", errors="replace")
        except OSError:
            return frozenset()
        return frozenset(s for s in text.split() if _SHA_RE.match(s))

    def _mark(self, info: CommitInfo) -> CommitInfo:
        if self._shallow and info.sha in self._shallow:
            return info.model_copy(update={"shallow_boundary": True})
        return info

    # ------------------------------------------------------------------ basic queries

    def head_sha(self) -> str | None:
        """Full sha of HEAD, or None for an empty repository."""
        out = self._run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"])
        sha = out.decode().strip() if out else ""
        return sha or None

    def ls_files(self) -> list[str]:
        """Paths of regular files tracked at HEAD (no symlinks or submodules), sorted."""
        out = self._run(["ls-tree", "-r", "-z", "--full-tree", "HEAD"])
        if not out:
            return []
        paths = []
        for entry in out.split(b"\0"):
            meta, tab, name = entry.partition(b"\t")
            fields = meta.split(b" ")
            if tab and len(fields) == 3 and fields[1] == b"blob" and fields[0] != b"120000":
                paths.append(name.decode("utf-8", "replace"))
        return paths

    def file_sizes(self) -> dict[str, int]:
        """{path: blob size in bytes} for the regular files tracked at HEAD (as `ls_files`),
        read from the tree without loading any blob."""
        out = self._run(["ls-tree", "-r", "-l", "-z", "--full-tree", "HEAD"])
        if not out:
            return {}
        sizes: dict[str, int] = {}
        for entry in out.split(b"\0"):
            meta, tab, name = entry.partition(b"\t")
            fields = meta.split()  # mode, type, object, size (right-aligned, space padded)
            if (
                tab
                and len(fields) == 4
                and fields[1] == b"blob"
                and fields[0] != b"120000"
                and fields[3].isdigit()
            ):
                sizes[name.decode("utf-8", "replace")] = int(fields[3])
        return sizes

    def read_file(self, path: str | Path, rev: str = "HEAD") -> str | None:
        """Text of `path` at `rev`, or None if it is missing there, not a file, or binary."""
        rel, r = self._rel(path), _check_rev(rev)
        if rel is None or r is None:
            return None
        data = self._run(["cat-file", "blob", f"{r}:{rel}"])
        if data is None or _is_binary(data):
            return None
        return data.decode("utf-8", "replace")

    def remote_slug(self) -> tuple[str, str] | None:
        """(owner, repo) of the GitHub remote: origin first, then upstream, then any other."""
        out = self._run(["remote", "-v"])
        if not out:
            return None
        urls: dict[str, str] = {}
        for line in out.decode("utf-8", "replace").splitlines():
            name, _, rest = line.partition("\t")
            if rest:
                urls.setdefault(name, rest.rsplit(" ", 1)[0])
        for name in sorted(urls, key=lambda n: (n != "origin", n != "upstream")):
            slug = parse_github_remote(urls[name])
            if slug:
                return slug
        return None

    # ------------------------------------------------------------------ commits

    def iter_commits(
        self,
        max_count: int | None = None,
        paths: Sequence[str] | None = None,
        *,
        timeout: float | None = None,
    ) -> Iterator[CommitInfo]:
        """Commits reachable from HEAD, newest first, with changed files (no diff).

        `paths` limits history to those paths; a single file path is followed across renames.
        Output is streamed, so stopping early is cheap. `timeout` (seconds) bounds the walk
        (`--follow` cannot use commit-graph filters and may walk most of a big history);
        when it expires the commits found so far are returned. None: no limit.
        """
        yield from self._log(max_count, paths, timeout=timeout)

    def show(self, sha: str, max_diff_chars: int = 6000) -> CommitInfo | None:
        """One commit with its patch (vs first parent), truncated to `max_diff_chars`.

        Returns None for an unknown revision. `max_diff_chars <= 0` skips the diff.
        """
        r = _check_rev(sha)
        if r is None:
            return None
        infos = self._commit_records(["-1", f"{r}^{{commit}}"])
        if not infos:
            return None
        info = infos[0]
        if max_diff_chars <= 0:
            return info
        args = ["show", "--format=", "--patch", "--no-ext-diff", "--no-textconv"]
        got = self._run_head([*args, *self._merge_args, info.sha, "--"], 4 * max_diff_chars + 4)
        if got is None:
            return info
        data, more = got
        diff = _truncate(data.decode("utf-8", "replace").strip("\n"), max_diff_chars, more)
        return info.model_copy(update={"diff": diff})

    def blame(self, path: str | Path, start: int, end: int, rev: str = "HEAD") -> list[BlameHunk]:
        """Who last changed lines `start..end` (1-based, inclusive) of `path` at `rev`.

        The range is clamped to the file. Honours a `.git-blame-ignore-revs` file at the
        repository root. Missing or binary files give [].
        """
        rel, r = self._rel(path), _check_rev(rev)
        if rel is None or r is None:
            return []
        text = self.read_file(rel, r)
        if text is None:
            return []
        start, end = max(1, start), min(end, _count_lines(text))
        if start > end:
            return []
        args = ["blame", "--porcelain", "-L", f"{start},{end}"]
        out = None
        ignore_file = self.root / ".git-blame-ignore-revs"
        if ignore_file.is_file():
            out = self._run([*args, "--ignore-revs-file", str(ignore_file), r, "--", rel])
        if out is None:
            out = self._run([*args, r, "--", rel])
        hunks = _parse_porcelain(out.decode("utf-8", "replace")) if out else []
        for hunk in hunks:
            hunk.shallow_boundary = hunk.sha in self._shallow
        return hunks

    def log_lines(
        self,
        path: str | Path,
        start: int,
        end: int,
        max_count: int = 10,
        *,
        max_diff_chars: int = 2000,
    ) -> list[CommitInfo]:
        """Commits that changed lines `start..end` of `path` (as at HEAD), newest first.

        Uses `git log -L`, which follows the lines through edits and file renames. Each commit's
        `diff` is the range-restricted patch, truncated to `max_diff_chars` (0 disables it).
        """
        rel = self._rel(path)
        text = self.read_file(rel) if rel is not None else None
        if rel is None or text is None or max_count <= 0:
            return []
        start, end = max(1, start), min(end, _count_lines(text))
        if start > end:
            return []
        cands = self._log_range(f"{start},{end}:{rel}", max_count)
        return self._hydrate(cands or [], max_diff_chars)

    def log_symbol(
        self,
        path: str | Path,
        symbol: str,
        max_count: int = 10,
        *,
        max_diff_chars: int = 2000,
        near: tuple[int, int] | None = None,
    ) -> list[CommitInfo]:
        """Commits that changed `symbol` (function, class, method or constant) in `path`.

        `symbol` may be qualified ("Retrier.run", "pkg.mod.Retrier.run") or bare ("run").
        Python files: the definition is located with `ast` at HEAD, traced with `git log -L`,
        and each candidate commit is kept only if the symbol's source actually changed in it;
        the walk stops where the symbol was introduced (following a rename when a vanished
        symbol of the same kind has near-identical source). Other files, or Python the parser
        rejects: `git log -L :<name>:<path>` with git's funcname drivers. If the symbol cannot
        be located at all, falls back to the plain history of `path` (bounded by `timeout`).

        `near=(start, end)`: the HEAD lines the caller means. When a Python name is defined
        more than once (property getter and setter, `@overload` stubs, `_` singledispatch
        implementations), the definition overlapping those lines is traced, not the first.
        """
        rel = self._rel(path)
        name = re.sub(r"\(.*\)\Z", "", symbol.strip())
        if rel is None or not name or max_count <= 0:
            return []
        text = self.read_file(rel)
        if text is not None and rel.endswith(_PYTHON_SUFFIXES):
            symbols = _python_symbols(text)
            qualname = _resolve_symbol(symbols, name) if symbols else None
            if symbols and qualname:
                if near is not None:
                    qualname = _definition_near(symbols, qualname, near)
                sym = symbols[qualname]
                cap = max(20, 3 * max_count)
                cands = self._log_range(f"{sym.start},{sym.end}:{rel}", cap)
                if cands is None:  # git failed on a valid range (e.g. timeout): don't retry -L
                    return list(self._log(max_count, [rel], timeout=self.timeout))
                kept = self._python_history(cands, rel, qualname, max_count)
                return self._hydrate(kept, max_diff_chars)
        regex = _funcname_regex(name)
        if text is not None and regex:
            cands = self._log_range(f":{regex}:{rel}", max_count)
            if cands is not None:
                return self._hydrate(cands, max_diff_chars)
        return list(self._log(max_count, [rel], timeout=self.timeout))

    def merged_commits(self, sha: str, max_count: int = 250) -> list[str]:
        """Full shas of the commits a merge commit brought in (`sha^1..sha^2`, the merged
        branch), newest first. [] when `sha` is unknown or not a merge commit (squash and
        rebase merges have a single parent)."""
        r = _check_rev(sha)
        if r is None or max_count <= 0:
            return []
        out = self._run(["rev-list", f"-n{max_count}", f"{r}^1..{r}^2", "--"])
        return [s for s in out.decode().split() if _SHA_RE.match(s)] if out else []

    def pickaxe(
        self, term: str, max_count: int = 10, paths: Sequence[str] | None = None
    ) -> list[CommitInfo]:
        """Commits that changed the number of occurrences of `term` (`git log -S`), newest
        first, optionally limited to `paths`."""
        if not term or max_count <= 0:
            return []
        return list(self._log(max_count, paths, extra=[f"-S{term}"]))

    # ------------------------------------------------------------------ internals

    def _cmd(self, args: Sequence[str]) -> list[str]:
        config = [*_GIT_CONFIG, f"core.attributesFile={self._attributes}"]
        return ["git", "-C", str(self.root), *(p for kv in config for p in ("-c", kv)), *args]

    def _run(self, args: Sequence[str], *, input: bytes | None = None) -> bytes | None:
        """stdout of a git command, or None on non-zero exit, timeout or OS error."""
        try:
            proc = subprocess.run(
                self._cmd(args),
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=self.timeout,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            log.warning("git %s timed out after %ss in %s", args[0], self.timeout, self.root)
            return None
        except (OSError, ValueError) as exc:  # ValueError: NUL byte in an argument
            log.debug("git %s could not run: %s", args[0], exc)
            return None
        if proc.returncode != 0:
            log.debug("git %s failed: %s", " ".join(args), proc.stderr.decode(errors="replace"))
            return None
        return proc.stdout

    def _run_head(self, args: Sequence[str], limit: int) -> tuple[bytes, bool] | None:
        """Read at most `limit` bytes of a command's stdout, killing git if there is more.

        Returns (data, truncated), or None if the command failed before producing it all.
        """
        try:
            proc = subprocess.Popen(
                self._cmd(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
            )
        except (OSError, ValueError):
            return None
        timer = threading.Timer(self.timeout, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            data = proc.stdout.read(limit + 1)
            truncated = len(data) > limit
            if truncated:
                proc.kill()
            returncode = proc.wait()
        finally:
            timer.cancel()
            _reap(proc)
        if not truncated and returncode != 0:
            return None
        return data[:limit], truncated

    def _stream(self, args: Sequence[str], timeout: float | None = None) -> Iterator[str]:
        """Yield the `\\x1e`-separated records of a git command's stdout as they arrive.

        With `timeout` (seconds), git is killed when it runs longer: the records completed by
        then are yielded and the cut-off last one is dropped (logged as a warning).
        """
        try:
            proc = subprocess.Popen(
                self._cmd(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
            )
        except (OSError, ValueError):
            return
        timed_out = threading.Event()
        timer: threading.Timer | None = None
        if timeout is not None:

            def kill() -> None:
                timed_out.set()
                proc.kill()

            timer = threading.Timer(timeout, kill)
            timer.daemon = True
            timer.start()
        try:
            assert proc.stdout is not None
            pending: list[bytes] = []
            while chunk := proc.stdout.read1(_STREAM_CHUNK):
                pieces = chunk.split(_RS.encode())
                if len(pieces) == 1:
                    pending.append(chunk)
                    continue
                pending.append(pieces[0])
                for raw in (b"".join(pending), *pieces[1:-1]):
                    if raw:
                        yield raw.decode("utf-8", "replace")
                pending = [pieces[-1]]
            tail = b"".join(pending)
            if timed_out.is_set() and proc.wait() != 0:
                log.warning("git %s timed out after %ss in %s", args[0], timeout, self.root)
            elif tail:
                yield tail.decode("utf-8", "replace")
        finally:
            if timer is not None:
                timer.cancel()
            _reap(proc)

    def _log(
        self,
        max_count: int | None,
        paths: Sequence[str] | str | None,
        extra: Sequence[str] = (),
        timeout: float | None = None,
    ) -> Iterator[CommitInfo]:
        if max_count is not None and max_count <= 0:
            return
        if isinstance(paths, str):
            paths = [paths]
        rels = [self._rel(p) for p in paths or []]
        if paths and not any(rels):
            return  # every path was outside the repository
        rels = [r for r in rels if r]
        args = ["log", "-z", f"--format={_LOG_FORMAT}", "--name-only", *self._merge_args, *extra]
        if max_count is not None:
            args.append(f"-n{max_count}")
        if len(rels) == 1 and self._is_file("HEAD", rels[0]):
            args.append("--follow")
        for rec in self._stream([*args, "--", *rels], timeout=timeout):
            info = _parse_log_record(rec)
            if info:
                yield self._mark(info)

    def _commit_records(self, revs: Sequence[str]) -> list[CommitInfo]:
        out = self._run(
            ["log", "-z", f"--format={_LOG_FORMAT}", "--name-only", *self._merge_args, *revs, "--"]
        )
        if not out:
            return []
        records = out.decode("utf-8", "replace").split(_RS)
        return [self._mark(info) for rec in records if (info := _parse_log_record(rec))]

    def _hydrate(self, cands: Sequence[_RangeCommit], max_diff_chars: int) -> list[CommitInfo]:
        """Full CommitInfo (metadata + files) for `git log -L` results, keeping their patch."""
        if not cands:
            return []
        records = self._commit_records(["--no-walk=unsorted", *(c.sha for c in cands)])
        infos = {info.sha: info for info in records}
        out = []
        for cand in cands:
            info = infos.get(cand.sha)
            if info is None:
                continue
            diff = _truncate(cand.diff, max_diff_chars) if max_diff_chars > 0 else None
            out.append(info.model_copy(update={"diff": diff}))
        return out

    def _log_range(self, spec: str, max_count: int) -> list[_RangeCommit] | None:
        """`git log -L <spec>` commits, newest first; None if git rejected the range."""
        out = self._run(["log", f"-L{spec}", f"--format={_RANGE_FORMAT}", f"-n{max_count}"])
        if out is None:
            return None
        commits: list[_RangeCommit] = []
        for rec in out.decode("utf-8", "replace").split(_RS):
            sha, sep, rest = rec.partition(_FS)
            if sep and _SHA_RE.match(sha):
                commits.append(_RangeCommit(sha, rest.strip("\n")))
            elif commits:  # a stray separator byte inside the patch text
                prev = commits[-1]
                commits[-1] = prev._replace(diff=prev.diff + _RS + rec.rstrip("\n"))
        return commits

    def _python_history(
        self, cands: Sequence[_RangeCommit], path: str, qualname: str, max_count: int
    ) -> list[_RangeCommit]:
        """Keep the `git log -L` candidates in which `qualname`'s source really changed.

        `git log -L` over-approximates: a hunk that replaced other code with this symbol keeps
        tracing the replaced lines into unrelated history. Comparing the symbol's source in
        each candidate and its first parent prunes those commits.
        """
        specs: list[tuple[str, str | None]] = []
        current = path
        for cand in cands:
            sides = _diff_paths(cand.diff)
            new_path, old_path = (sides[1] or current, sides[0]) if sides else (current, current)
            parent_spec = f"{cand.sha}^:{old_path}" if old_path else None
            specs.append((f"{cand.sha}:{new_path}", parent_spec))
            current = old_path or new_path
        blobs = self._read_blobs([s for pair in specs for s in pair if s])
        if blobs is None:
            return list(cands[:max_count])
        parsed: dict[str, dict[str, _Symbol] | None] = {}

        def symbols_of(text: str | None) -> dict[str, _Symbol] | None:
            if text is None:
                return {}  # no such file in that revision
            if text not in parsed:
                parsed[text] = _python_symbols(text)
            return parsed[text]

        kept: list[_RangeCommit] = []
        name = qualname
        for cand, (cur_spec, par_spec) in zip(cands, specs, strict=True):
            cur_text = blobs.get(cur_spec)
            par_text = blobs.get(par_spec) if par_spec else None
            cur_syms, par_syms = symbols_of(cur_text), symbols_of(par_text)
            if cur_syms is None or par_syms is None:  # unparseable version: trust git -L
                kept.append(cand)
            else:
                if name not in cur_syms:
                    # e.g. the enclosing class was renamed after this commit
                    name = _resolve_symbol(cur_syms, name.rsplit(".", 1)[-1]) or ""
                    if not name:
                        break  # the traced lines no longer hold the symbol
                cur_src = _source(cur_text or "", cur_syms[name])
                if name in par_syms and _source(par_text or "", par_syms[name]) == cur_src:
                    continue
                kept.append(cand)
                if name not in par_syms:
                    renamed = _renamed_from(
                        par_text, par_syms, cur_syms, cur_src, cur_syms[name], name
                    )
                    if renamed is None:
                        break  # introduced here
                    name = renamed
            if len(kept) >= max_count:
                break
        return kept or list(cands[:max_count])

    def _read_blobs(self, specs: Sequence[str]) -> dict[str, str | None] | None:
        """Text of several `<rev>:<path>` blobs in one `git cat-file --batch` call.

        Missing, non-blob and binary objects map to None; None overall if git failed.
        """
        result: dict[str, str | None] = dict.fromkeys(specs)
        wanted = [s for s in dict.fromkeys(specs) if "\n" not in s]
        if not wanted:
            return result
        out = self._run(["cat-file", "--batch"], input="".join(f"{s}\n" for s in wanted).encode())
        if out is None:
            return None
        pos = 0
        for spec in wanted:
            eol = out.find(b"\n", pos)
            if eol < 0:
                break
            header = out[pos:eol].decode("utf-8", "replace")
            pos = eol + 1
            fields = header.split(" ")
            if len(fields) != 3 or not _SHA_RE.match(fields[0]) or not fields[2].isdigit():
                continue  # "<spec> missing" / "<spec> ambiguous"
            size = int(fields[2])
            data = out[pos : pos + size]
            pos += size + 1
            if fields[1] == "blob" and not _is_binary(data):
                result[spec] = data.decode("utf-8", "replace")
        return result

    def _is_file(self, rev: str, rel: str) -> bool:
        out = self._run(["cat-file", "-t", f"{rev}:{rel}"])
        return out is not None and out.strip() == b"blob"

    def _rel(self, path: str | Path) -> str | None:
        """Repository-relative POSIX path, or None if it points outside the repository."""
        raw = str(path).strip()
        if not raw or "\0" in raw:
            return None
        if os.path.isabs(raw):
            raw = self._relative_to_root(Path(raw))
        rel = posixpath.normpath(raw)
        if rel in ("", ".", "..") or rel.startswith("../"):
            return None
        return rel

    def _relative_to_root(self, path: Path) -> str:
        """Absolute path -> repo-relative, tolerating symlinked prefixes (/tmp vs /private/tmp).
        An absolute path outside the repo is read as repo-relative ("/src/x.py" from a model)."""
        bases = (self.root, self.root.resolve())
        for candidate in (path, Path(os.path.realpath(path.parent)) / path.name):
            for base in bases:
                if candidate.is_relative_to(base):
                    return candidate.relative_to(base).as_posix()
        return path.as_posix().lstrip("/")


# ---------------------------------------------------------------------- helpers


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"  # never take index.lock: safe alongside other git users
    env["GIT_TERMINAL_PROMPT"] = "0"  # never block on credential prompts
    return env


def _attributes_file() -> str:
    """Path of a process-wide private attributes file mapping extensions to diff drivers."""
    global _ATTR_PATH
    with _ATTR_LOCK:
        if _ATTR_PATH is None or not os.path.exists(_ATTR_PATH):
            fd, path = tempfile.mkstemp(prefix="archaeologist-", suffix=".gitattributes")
            with os.fdopen(fd, "w") as fh:
                fh.write(_DIFF_ATTRIBUTES)
            atexit.register(_remove_quietly, path)
            _ATTR_PATH = path
        return _ATTR_PATH


def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _git_version() -> tuple[int, int]:
    global _GIT_VERSION
    with _VERSION_LOCK:
        if _GIT_VERSION is None:
            try:
                out = subprocess.run(
                    ["git", "--version"], capture_output=True, text=True, timeout=10, check=False
                ).stdout
            except (OSError, subprocess.TimeoutExpired):
                out = ""
            m = re.search(r"(\d+)\.(\d+)", out)
            _GIT_VERSION = (int(m[1]), int(m[2])) if m else (0, 0)
        return _GIT_VERSION


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    if proc.stdout is not None:
        proc.stdout.close()
    proc.wait()


def _check_rev(rev: str) -> str | None:
    """A revision safe to pass to git (never parsed as an option), or None."""
    rev = rev.strip()
    if not rev or rev.startswith("-") or any(c in rev for c in "\0\n\r"):
        return None
    return rev


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:_BINARY_SNIFF]


def _count_lines(text: str) -> int:
    """Number of lines as git counts them (newline-terminated, last line may lack one)."""
    return text.count("\n") + (1 if text and not text.endswith("\n") else 0)


def _iso(date: str) -> str:
    try:
        return datetime.fromisoformat(date).isoformat()
    except ValueError:
        return date


def _epoch_iso(seconds: str, tz: str) -> str:
    m = re.fullmatch(r"([+-])(\d\d)(\d\d)", tz.strip())
    offset = timedelta(0)
    if m:
        offset = timedelta(hours=int(m[2]), minutes=int(m[3])) * (-1 if m[1] == "-" else 1)
    try:
        return datetime.fromtimestamp(int(seconds), timezone(offset)).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _truncate(text: str, limit: int, more: bool = False) -> str:
    if len(text) <= limit and not more:
        return text
    return text[:limit].rstrip() + _TRUNCATED


def _parse_log_record(rec: str) -> CommitInfo | None:
    """Parse one `_LOG_FORMAT` record followed by `-z --name-only` file names."""
    parts = rec.split(_FS, 6)
    if len(parts) != 7 or not _SHA_RE.match(parts[0]):
        return None
    sha, short, author, date, subject, body, tail = parts
    files = [f for f in tail.lstrip("\0\n").split("\0") if f.strip("\n")]
    return CommitInfo(
        sha=sha,
        short_sha=short,
        author=author,
        date=_iso(date),
        subject=subject,
        body=body.strip(),
        files=files,
    )


def _parse_porcelain(out: str) -> list[BlameHunk]:
    """Turn `git blame --porcelain` output into hunks of consecutive same-commit lines."""
    meta: dict[str, dict[str, str]] = {}
    lines: list[tuple[str, int]] = []
    sha: str | None = None
    final = 0
    for line in out.split("\n"):
        if line.startswith("\t"):
            if sha:
                lines.append((sha, final))
            continue
        fields = line.split(" ")
        if (
            len(fields) in (3, 4)
            and _SHA_RE.match(fields[0])
            and fields[1].isdigit()
            and fields[2].isdigit()
        ):
            sha, final = fields[0], int(fields[2])
            meta.setdefault(sha, {})
        elif sha:
            key, _, value = line.partition(" ")
            meta[sha].setdefault(key, value)
    hunks: list[BlameHunk] = []
    for sha, n in lines:
        if hunks and hunks[-1].sha == sha and hunks[-1].end == n - 1:
            hunks[-1].end = n
            continue
        m = meta[sha]
        hunks.append(
            BlameHunk(
                sha=sha,
                start=n,
                end=n,
                author=m.get("author", ""),
                date=_epoch_iso(m.get("author-time", "0"), m.get("author-tz", "+0000")),
                summary=m.get("summary", ""),
            )
        )
    return hunks


def _unquote(name: str) -> str:
    """Undo git's C-style quoting of unusual path names ("a\\tb" -> a<TAB>b)."""
    if len(name) >= 2 and name[0] == name[-1] == '"':
        raw = codecs.escape_decode(name[1:-1].encode("utf-8"))[0]
        return raw.decode("utf-8", "replace")
    return name


def _diff_paths(diff: str) -> tuple[str | None, str | None] | None:
    """(old, new) paths from a patch's ---/+++ header; None for /dev/null. None if no header."""
    old: str | None = None
    seen_old = False
    for line in diff.split("\n"):
        if line.startswith("--- ") and not seen_old:
            old, seen_old = _diff_side(line[4:], "a/"), True
        elif line.startswith("+++ ") and seen_old:
            return old, _diff_side(line[4:], "b/")
    return None


def _diff_side(raw: str, prefix: str) -> str | None:
    raw = raw.rstrip("\t")
    if raw == "/dev/null":
        return None
    return _unquote(raw).removeprefix(prefix)


def _funcname_regex(symbol: str) -> str | None:
    """POSIX basic regex for `git log -L :<regex>:<file>` matching a definition line that
    contains the symbol's last component as a whole identifier."""
    name = re.split(r"\.|::", symbol)[-1]
    if not name or any(c in name for c in ":\n\r\0"):
        return None  # ':' would end git's funcname argument early
    escaped = re.sub(r"([.\[\]\\*^$])", r"\\\1", name)
    ident = "[^A-Za-z0-9_$]"
    return rf"^\(.*{ident}\)*{escaped}\({ident}.*\)*$"


def _silence_syntax_warnings() -> None:
    for action, _msg, category, module, _lineno in warnings.filters:
        if (
            action == "ignore"
            and category is SyntaxWarning
            and module is not None
            and module.pattern == _AST_WARNING_MODULE
        ):
            return
    warnings.filterwarnings("ignore", category=SyntaxWarning, module=_AST_WARNING_MODULE)


def _python_symbols(text: str) -> dict[str, _Symbol] | None:
    """Qualified name -> line range of every def/class (any depth) and module/class-level
    assignment. None if the source does not parse.

    A name defined more than once (property getter/setter, `@overload` stubs, re-assigned
    constants) keeps its first definition under the plain name and later ones under
    "<name>#2", "<name>#3", ... in source order, so the n-th definition can be traced across
    revisions."""
    _silence_syntax_warnings()
    try:
        tree = ast.parse(text, filename=_AST_FILENAME)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    symbols: dict[str, _Symbol] = {}

    def add(qualname: str, sym: _Symbol) -> None:
        key, n = qualname, 1
        while key in symbols:
            n += 1
            key = f"{qualname}{_DUP}{n}"
        symbols[key] = sym

    def visit(body: list[ast.stmt], prefix: str, in_function: bool) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualname = prefix + node.name
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                is_class = isinstance(node, ast.ClassDef)
                kind = "class" if is_class else "def"
                add(qualname, _Symbol(first, node.end_lineno or node.lineno, kind))
                visit(node.body, qualname + ".", in_function or not is_class)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not in_function:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        end = node.end_lineno or node.lineno
                        add(prefix + target.id, _Symbol(node.lineno, end, "assign"))
            elif isinstance(node, (ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith)):
                # definitions under `if TYPE_CHECKING:`, `try: ... except ImportError:`, ...
                blocks = [getattr(node, f, []) for f in ("body", "orelse", "finalbody")]
                blocks += [h.body for h in getattr(node, "handlers", [])]
                for block in blocks:
                    visit(block, prefix, in_function)

    visit(tree.body, "", False)
    return symbols


def _resolve_symbol(symbols: dict[str, _Symbol], symbol: str) -> str | None:
    """Match a user-supplied symbol to a qualified name: exact, then without leading
    module/package components, then any definition whose last component matches."""
    parts = [p for p in re.split(r"\.|::", symbol) if p]
    if not parts:
        return None
    for i in range(len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in symbols:
            return candidate
    tail = parts[-1]
    matches = [q for q in symbols if q.rsplit(".", 1)[-1] == tail]
    return min(matches, key=lambda q: symbols[q].start) if matches else None


def _base_name(key: str) -> str:
    """Symbol-table key without its duplicate suffix ("Box.size#2" -> "Box.size")."""
    return key.split(_DUP, 1)[0]


def _definition_near(symbols: dict[str, _Symbol], qualname: str, near: tuple[int, int]) -> str:
    """Among the definitions of `qualname` (it and its "#n" duplicates), the one overlapping
    lines `near` the most; `qualname` itself when none overlaps."""
    lo, hi = near[0], max(near)
    base = _base_name(qualname)
    best, best_overlap = qualname, 0
    for key, sym in symbols.items():
        if _base_name(key) != base:
            continue
        overlap = min(hi, sym.end) - max(lo, sym.start) + 1
        if overlap > best_overlap:
            best, best_overlap = key, overlap
    return best


def _source(text: str, sym: _Symbol) -> str:
    return "\n".join(text.split("\n")[sym.start - 1 : sym.end])


def _tail_name(key: str) -> str:
    return _base_name(key).rsplit(".", 1)[-1]


def _similarity_matcher(old: str, new: str) -> difflib.SequenceMatcher:
    """Character-level for small sources (exact, cheap there); line-level above
    `_CHAR_COMPARE_MAX` characters, where character matching grows roughly quadratically
    (seconds per comparison for a few hundred lines)."""
    if len(old) + len(new) <= _CHAR_COMPARE_MAX:
        return difflib.SequenceMatcher(None, old, new, autojunk=False)
    return difflib.SequenceMatcher(None, old.split("\n"), new.split("\n"), autojunk=False)


def _renamed_from(
    par_text: str | None,
    par_syms: dict[str, _Symbol],
    cur_syms: dict[str, _Symbol],
    cur_src: str,
    cur_sym: _Symbol,
    cur_name: str = "",
) -> str | None:
    """Name the symbol had in the parent if it was renamed rather than newly written: a
    same-kind symbol that vanished in this commit and whose source is near-identical once
    the old name is replaced by the new one. Cheap upper bounds (length, then character or
    line multiset) skip candidates that cannot beat the best match so far."""
    if not par_text:
        return None
    new_tail = _tail_name(cur_name) if cur_name else ""
    best, best_ratio = None, 0.0
    for qualname, sym in par_syms.items():
        if qualname in cur_syms or sym.kind != cur_sym.kind:
            continue
        old_src = _source(par_text, sym)
        old_tail = _tail_name(qualname)
        if new_tail and old_tail != new_tail:
            old_src = re.sub(rf"\b{re.escape(old_tail)}\b", new_tail, old_src)
        matcher = _similarity_matcher(old_src, cur_src)
        floor = max(best_ratio, _SYMBOL_RENAME_RATIO)
        if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
            continue
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best, best_ratio = qualname, ratio
    return best if best_ratio >= _SYMBOL_RENAME_RATIO else None
