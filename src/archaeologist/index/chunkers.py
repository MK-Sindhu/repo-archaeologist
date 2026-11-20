"""Turn source files and commits into index `Chunk`s.

Chunk ids are canonical refs (see refs.py):

- code and plain-text windows: ``code:<path>#L<a>-L<b>``
- markdown / reStructuredText sections: ``doc:<path>#<slug>`` (duplicate slugs get -2, -3, ...)
- commits: ``commit:<sha>``

Code chunk text starts with a header line ``<path>:<start>-<end> <symbol>`` so a search hit
carries its location even when shown out of context.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import PurePosixPath
from typing import Literal, Protocol

from ..models import Chunk
from ..refs import code_ref, commit_ref, doc_ref, extract_refs, slugify

ChunkKind = Literal["code", "doc"]
# (first line index, last line index, level, heading text, explicit anchor) - 0-based lines
_Heading = tuple[int, int, int, str, str | None]

PYTHON_EXTS = frozenset({".py", ".pyi"})
MARKDOWN_EXTS = frozenset({".md", ".markdown", ".mdx"})
RST_EXTS = frozenset({".rst"})
PLAIN_DOC_EXTS = frozenset({".txt"})

DEFAULT_WINDOW = 60
DEFAULT_OVERLAP = 10
DEFAULT_MAX_LINES = 200  # python definitions longer than this are split into windows

_MAX_COMMIT_BODY_CHARS = 8000
_MAX_COMMIT_FILES = 50
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class CommitLike(Protocol):
    """What `commit_chunk` reads from a commit (sources.git.CommitInfo satisfies it)."""

    @property
    def sha(self) -> str: ...

    @property
    def short_sha(self) -> str: ...

    @property
    def author(self) -> str: ...

    @property
    def date(self) -> str: ...  # ISO-8601

    @property
    def subject(self) -> str: ...

    @property
    def body(self) -> str: ...

    @property
    def files(self) -> Sequence[str]: ...


# -- helpers ---------------------------------------------------------------------------------


def _split_lines(source: str) -> list[str]:
    """Split on the same line breaks Python's tokenizer uses (not str.splitlines, which also
    breaks on form feeds and other characters and would shift ast line numbers)."""
    lines = _LINE_BREAK.split(source)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


# Public name: code:<path>#L<a>-L<b> fetches must number lines exactly like these chunkers.
split_lines = _split_lines


def _header(path: str, start: int, end: int, symbol: str | None = None) -> str:
    return f"{path}:{start}-{end}" + (f" {symbol}" if symbol else "")


def _window_ranges(start: int, end: int, window: int, overlap: int) -> list[tuple[int, int]]:
    """Inclusive 1-based line ranges covering [start, end] with `window` lines and `overlap`."""
    if window < 1 or not 0 <= overlap < window:
        raise ValueError(f"need window >= 1 and 0 <= overlap < window (got {window}, {overlap})")
    ranges = []
    step = window - overlap
    a = start
    while True:
        b = min(a + window - 1, end)
        ranges.append((a, b))
        if b >= end:
            return ranges
        a += step


def _code_chunk(
    path: str,
    lines: list[str],
    start: int,
    end: int,
    *,
    symbol: str | None,
    node: str,
    title: str,
    body: str | None = None,
    kind: ChunkKind = "code",
    meta: dict | None = None,
) -> Chunk:
    text = body if body is not None else "\n".join(lines[start - 1 : end])
    label = symbol if symbol else (node if node == "module" else None)
    return Chunk(
        id=code_ref(path, start, end),
        kind=kind,
        title=title,
        text=f"{_header(path, start, end, label)}\n{text}",
        path=path,
        start_line=start,
        end_line=end,
        symbol=symbol,
        meta={"node": node, **(meta or {})},
    )


def _windows(
    path: str,
    lines: list[str],
    start: int,
    end: int,
    *,
    window: int,
    overlap: int,
    symbol: str | None,
    node: str,
    title: str,
    kind: ChunkKind = "code",
) -> list[Chunk]:
    """Window [start, end] into chunks, dropping windows that are only whitespace."""
    ranges = _window_ranges(start, end, window, overlap)
    out = []
    for part, (a, b) in enumerate(ranges, 1):
        if not any(line.strip() for line in lines[a - 1 : b]):
            continue
        split = len(ranges) > 1
        meta = {"part": part, "parts": len(ranges)} if split else {}
        out.append(
            _code_chunk(
                path,
                lines,
                a,
                b,
                symbol=symbol,
                node=node,
                title=f"{title} (lines {a}-{b})" if split else title,
                kind=kind,
                meta=meta,
            )
        )
    return out


# -- plain text ------------------------------------------------------------------------------


def chunk_text(
    path: str,
    source: str,
    window: int = DEFAULT_WINDOW,
    overlap: int = DEFAULT_OVERLAP,
    *,
    kind: ChunkKind = "code",
) -> list[Chunk]:
    """Sliding line windows over any text file (fallback for non-Python languages).

    Each chunk covers `window` lines and shares `overlap` lines with the previous one;
    whitespace-only windows are dropped. Raises ValueError unless 0 <= overlap < window.
    """
    lines = _split_lines(source)
    if not lines:
        _window_ranges(1, 1, window, overlap)  # still validate the arguments
        return []
    return _windows(
        path,
        lines,
        1,
        len(lines),
        window=window,
        overlap=overlap,
        symbol=None,
        node="window",
        title=path,
        kind=kind,
    )


# -- python ----------------------------------------------------------------------------------

_DefNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _def_start(node: _DefNode, lines: list[str], floor: int) -> int:
    """First line of a definition: its decorators plus the comment block directly above it
    (never reaching back to line `floor` or earlier, which belongs to the previous statement)."""
    start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    while start - 1 > floor and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _body_indent(node: _DefNode, lines: list[str]) -> str:
    first = lines[node.body[0].lineno - 1]
    return first[: len(first) - len(first.lstrip())]


def _signature_lines(node: _DefNode, lines: list[str]) -> list[str]:
    """Decorators + the `def`/`class` line(s) of a definition, followed by its docstring
    summary (or `...`). One-line definitions are returned as they are."""
    head_start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    body_line = node.body[0].lineno
    if body_line == node.lineno:  # `def f(): return 1` / `class E(Exception): pass`
        return lines[head_start - 1 : node.end_lineno]
    out = lines[head_start - 1 : body_line - 1]
    indent = _body_indent(node, lines)
    doc = ast.get_docstring(node)
    summary = doc.strip().splitlines()[0].strip() if doc and doc.strip() else ""
    out.append(f'{indent}"""{summary}"""' if summary else f"{indent}...")
    return out


def _stmt_lines(stmt: ast.stmt, lines: list[str], max_lines: int) -> list[str]:
    """Source of a statement, cut to `max_lines` with an indented `...` marker."""
    seg = lines[stmt.lineno - 1 : stmt.end_lineno]
    if len(seg) <= max_lines:
        return seg
    indent = seg[0][: len(seg[0]) - len(seg[0].lstrip())]
    return [*seg[:max_lines], f"{indent}..."]


def _class_summary(node: ast.ClassDef, lines: list[str]) -> str:
    """Class header, docstring and member signatures; method bodies are left to their own
    chunks."""
    body_line = node.body[0].lineno
    if body_line == node.lineno:
        return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    head_start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    out = lines[head_start - 1 : body_line - 1]
    for i, stmt in enumerate(node.body):
        if i == 0 and _is_docstring(stmt):
            out.extend(lines[stmt.lineno - 1 : stmt.end_lineno])
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            out.extend(_signature_lines(stmt, lines))
        elif isinstance(stmt, ast.Assign | ast.AnnAssign | ast.AugAssign):
            out.extend(_stmt_lines(stmt, lines, 5))
        else:
            out.extend(_stmt_lines(stmt, lines, 10))
    return "\n".join(out)


class _PythonChunker:
    def __init__(self, path: str, lines: list[str], window: int, overlap: int, max_lines: int):
        self.path = path
        self.lines = lines
        self.window = window
        self.overlap = overlap
        self.max_lines = max_lines
        self.chunks: list[Chunk] = []

    def definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, symbol: str, node_type: str, floor: int
    ) -> None:
        start = _def_start(node, self.lines, floor)
        end = node.end_lineno or node.lineno
        title = f"{symbol} in {self.path}"
        if end - start + 1 > self.max_lines:
            self.chunks.extend(
                _windows(
                    self.path,
                    self.lines,
                    start,
                    end,
                    window=self.window,
                    overlap=self.overlap,
                    symbol=symbol,
                    node=node_type,
                    title=title,
                )
            )
        else:
            self.chunks.append(
                _code_chunk(
                    self.path, self.lines, start, end, symbol=symbol, node=node_type, title=title
                )
            )

    def klass(self, node: ast.ClassDef, symbol: str, floor: int) -> None:
        start = _def_start(node, self.lines, floor)
        end = node.end_lineno or node.lineno
        self.chunks.append(
            _code_chunk(
                self.path,
                self.lines,
                start,
                end,
                symbol=symbol,
                node="class",
                title=f"class {symbol} in {self.path}",
                body=_class_summary(node, self.lines),
            )
        )
        prev_end = node.lineno
        for stmt in node.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.definition(stmt, f"{symbol}.{stmt.name}", "method", prev_end)
            elif isinstance(stmt, ast.ClassDef):
                self.klass(stmt, f"{symbol}.{stmt.name}", prev_end)
            prev_end = stmt.end_lineno or stmt.lineno

    def module(self, tree: ast.Module) -> None:
        segments: list[tuple[int, int]] = []  # runs of top-level statements that are not defs
        prev_end = 0
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                self.definition(stmt, stmt.name, "function", prev_end)
            elif isinstance(stmt, ast.ClassDef):
                self.klass(stmt, stmt.name, prev_end)
            else:
                end = stmt.end_lineno or stmt.lineno
                if segments and segments[-1][1] == prev_end:
                    segments[-1] = (segments[-1][0], end)
                else:
                    segments.append((stmt.lineno, end))
            prev_end = stmt.end_lineno or stmt.lineno
        self._module_chunks(segments)

    def _module_chunks(self, segments: list[tuple[int, int]]) -> None:
        """Group runs of module-level statements (docstring, imports, constants, script code)
        into small "module" chunks whose line range spans at most `window` lines; the text
        holds only those statements (definitions in between are elided as "...")."""
        title = f"{self.path} (module)"
        group: list[tuple[int, int]] = []

        def flush() -> None:
            if not group:
                return
            body = "\n...\n".join("\n".join(self.lines[a - 1 : b]) for a, b in group)
            self.chunks.append(
                _code_chunk(
                    self.path,
                    self.lines,
                    group[0][0],
                    group[-1][1],
                    symbol=None,
                    node="module",
                    title=title,
                    body=body,
                )
            )
            group.clear()

        for a, b in segments:
            if b - a + 1 > self.window:
                flush()
                self.chunks.extend(
                    _windows(
                        self.path,
                        self.lines,
                        a,
                        b,
                        window=self.window,
                        overlap=self.overlap,
                        symbol=None,
                        node="module",
                        title=title,
                    )
                )
                continue
            if group and b - group[0][0] + 1 > self.window:
                flush()
            group.append((a, b))
        flush()


def chunk_python(
    path: str,
    source: str,
    *,
    window: int = DEFAULT_WINDOW,
    overlap: int = DEFAULT_OVERLAP,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[Chunk]:
    """Chunk Python source with `ast`.

    - each module-level function and each method is its own chunk, with a qualified symbol
      ("backoff", "Retrier.run", "Outer.Inner.method"); decorators and the comment block
      directly above a definition are included;
    - each class gets a summary chunk (header, docstring, attributes, member signatures)
      spanning the whole class;
    - module docstring, imports, constants and other top-level code become small "module"
      chunks (symbol None);
    - definitions longer than `max_lines` are split into overlapping windows that keep the
      symbol.

    Falls back to `chunk_text` when the source does not parse.
    """
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return chunk_text(path, source, window, overlap)
    lines = _split_lines(source)
    chunker = _PythonChunker(path, lines, window, overlap, max_lines)
    chunker.module(tree)
    ordered = sorted(chunker.chunks, key=lambda c: (c.start_line or 0, -(c.end_line or 0)))
    return _dedupe(ordered)


# -- markdown / rst --------------------------------------------------------------------------

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_CUSTOM_ID = re.compile(r"\s*\{#([\w-]+)\}\s*$")
_RST_ADORN = re.compile(r"^([=\-`:'\"~^_*+#<>.])\1{2,}\s*$")
_NOT_PARAGRAPH = re.compile(r"^ {0,3}(?:[>|<]|[-*+][ \t]|\d{1,9}[.)][ \t])")


def _clean_heading(text: str) -> tuple[str, str | None]:
    """Strip inline markup from a heading; return (text, explicit {#id} anchor or None)."""
    custom = None
    m = _CUSTOM_ID.search(text)
    if m:
        custom = m.group(1)
        text = text[: m.start()]
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    return text.strip(), custom


def _markdown_headings(lines: list[str]) -> list[_Heading]:
    """Markdown headings, skipping fenced code blocks and YAML front matter."""
    out: list[_Heading] = []
    fence: str | None = None
    i = 0
    # YAML front matter is never a heading
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                i = j + 1
                break
    prev_text = False  # previous line is paragraph text (setext candidate)
    for idx in range(i, len(lines)):
        line = lines[idx]
        m = _FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            prev_text = False
            continue
        if m:
            fence = m.group(1)
            prev_text = False
            continue
        atx = _ATX.match(line)
        if atx:
            text, custom = _clean_heading(atx.group(2) or "")
            out.append((idx, idx, len(atx.group(1)), text, custom))
            prev_text = False
            continue
        st = _SETEXT.match(line)
        if st and prev_text and not lines[idx - 1].startswith((" " * 4, "\t")):
            text, custom = _clean_heading(lines[idx - 1])
            out.append((idx - 1, idx, 1 if st.group(1)[0] == "=" else 2, text, custom))
            prev_text = False
            continue
        prev_text = bool(line.strip()) and not _NOT_PARAGRAPH.match(line)
    return out


def _rst_headings(lines: list[str]) -> list[_Heading]:
    """reStructuredText section titles (underlined, optionally overlined). Levels follow the
    order in which adornment styles first appear, as docutils does."""
    out: list[_Heading] = []
    styles: list[tuple[str, bool]] = []

    def record(first: int, last: int, char: str, overlined: bool, text: str) -> None:
        style = (char, overlined)
        if style not in styles:
            styles.append(style)
        out.append((first, last, styles.index(style) + 1, text, None))

    def is_title(line: str) -> bool:
        return bool(line.strip()) and not _RST_ADORN.match(line)

    n = len(lines)
    idx = 0
    while idx < n:
        after_blank = idx == 0 or not lines[idx - 1].strip()
        over = _RST_ADORN.match(lines[idx])
        if (
            over
            and idx + 2 < n
            and is_title(lines[idx + 1])
            and lines[idx + 2].strip() == lines[idx].strip()
        ):
            record(idx, idx + 2, over.group(1), True, lines[idx + 1].strip())
            idx += 3
            continue
        if (
            after_blank
            and idx + 1 < n
            and is_title(lines[idx])
            and not lines[idx].startswith((" ", "\t"))
            and (under := _RST_ADORN.match(lines[idx + 1]))
            and len(lines[idx + 1].rstrip()) >= len(lines[idx].rstrip())
        ):
            record(idx, idx + 1, under.group(1), False, lines[idx].strip())
            idx += 2
            continue
        idx += 1
    return out


def _unique_slug(base: str, used: set[str]) -> str:
    slug, n = base, 1
    while slug in used:
        n += 1
        slug = f"{base}-{n}"
    used.add(slug)
    return slug


def _sections(
    path: str, source: str, find_headings: Callable[[list[str]], list[_Heading]]
) -> list[Chunk]:
    lines = _split_lines(source)
    if not lines:
        return []
    headings = find_headings(lines)
    used: set[str] = set()
    chunks: list[Chunk] = []

    def emit(first: int, last: int, slug: str, title: str, trail: list[str], level: int) -> None:
        body = "\n".join(lines[first : last + 1]).strip("\n")
        if not body.strip():
            return
        crumb = " > ".join([path, *trail])
        chunks.append(
            Chunk(
                id=doc_ref(path, slug),
                kind="doc",
                title=title,
                text=f"{crumb}\n{body}",
                path=path,
                start_line=first + 1,
                end_line=last + 1,
                meta={"slug": slug, "level": level, "breadcrumb": trail},
            )
        )

    first_heading = headings[0][0] if headings else len(lines)
    if any(line.strip() for line in lines[:first_heading]):
        emit(0, first_heading - 1, _unique_slug("top", used), PurePosixPath(path).name, [], 0)

    stack: list[tuple[int, str]] = []  # (level, heading text) of enclosing sections
    for n, (first, _last, level, text, custom) in enumerate(headings):
        end = headings[n + 1][0] - 1 if n + 1 < len(headings) else len(lines) - 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text or "section"))
        slug = _unique_slug(custom or slugify(text), used)
        emit(first, end, slug, text or slug, [t for _, t in stack], level)
    return chunks


def chunk_markdown(path: str, source: str) -> list[Chunk]:
    """One "doc" chunk per markdown heading (ATX or setext; headings inside code fences and
    YAML front matter are ignored). Ids are ``doc:<path>#<slug>`` with GitHub-style slugs;
    a repeated slug becomes ``<slug>-2``, ``<slug>-3``. Text before the first heading becomes
    section ``top``. Each chunk's text starts with a breadcrumb line
    ``<path> > <h1> > <h2> ...``."""
    return _sections(path, source, _markdown_headings)


def chunk_rst(path: str, source: str) -> list[Chunk]:
    """Like `chunk_markdown`, for reStructuredText section titles."""
    return _sections(path, source, _rst_headings)


# -- commits ---------------------------------------------------------------------------------


def commit_chunk(commit: CommitLike) -> Chunk:
    """A "commit" chunk: subject, body and changed files, id ``commit:<sha>``.

    `meta` carries sha, short_sha, author, files and the refs found in the message
    (e.g. "pr:12" for a squash subject, "issue:7" for "Fixes #7")."""
    subject = (commit.subject or "").strip()
    body = (commit.body or "").strip()
    if len(body) > _MAX_COMMIT_BODY_CHARS:
        body = body[:_MAX_COMMIT_BODY_CHARS] + "\n…[truncated]"
    files = list(commit.files or [])
    shown = files[:_MAX_COMMIT_FILES]
    file_text = "\n".join(shown)
    if len(files) > len(shown):
        file_text += f"\n… (+{len(files) - len(shown)} more files)"
    date = commit.date or None
    head = f"commit {commit.short_sha} by {commit.author}" + (f" on {date[:10]}" if date else "")
    parts = [head, subject]
    if body:
        parts.append(body)
    if file_text:
        parts.append(f"files:\n{file_text}")
    return Chunk(
        id=commit_ref(commit.sha),
        kind="commit",
        title=subject or commit.short_sha,
        text="\n\n".join(parts),
        date=date,
        meta={
            "sha": commit.sha,
            "short_sha": commit.short_sha,
            "author": commit.author,
            "files": shown,
            "refs": extract_refs(f"{subject}\n\n{commit.body or ''}"),
        },
    )


# -- dispatch --------------------------------------------------------------------------------


def chunk_file(path: str, source: str) -> list[Chunk]:
    """Pick the chunker for a file by extension: Python -> `chunk_python`, markdown ->
    `chunk_markdown`, rst -> `chunk_rst`, .txt -> `chunk_text` as docs, anything else ->
    `chunk_text` as code."""
    ext = PurePosixPath(path).suffix.lower()
    if ext in PYTHON_EXTS:
        return chunk_python(path, source)
    if ext in MARKDOWN_EXTS:
        return chunk_markdown(path, source)
    if ext in RST_EXTS:
        return chunk_rst(path, source)
    if ext in PLAIN_DOC_EXTS:
        return chunk_text(path, source, kind="doc")
    return chunk_text(path, source)


def _dedupe(chunks: Iterable[Chunk]) -> list[Chunk]:
    seen: set[str] = set()
    out = []
    for c in chunks:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out
