"""Outlines of source files: the functions, classes, methods and types they define.

`outline(path, source)` works for Python (with `ast`) and, when the optional "outline" extra
is installed (`tree-sitter` plus pinned grammar wheels), for TypeScript/TSX, JavaScript, Go,
Rust, Java, C#, Ruby, PHP, C and C++. It never raises: an unknown language, a missing
grammar, a source tree-sitter would number differently from our line splitter (lone ``\\r``
line breaks), or a parse that is mostly errors gives an outline with no symbols.

Qualified names follow the Python chunker: ``backoff``, ``Retrier.run``,
``Outer.Inner.method``; Go methods are ``Type.Method``, Rust impl methods ``Type.method``,
C++ out-of-line definitions keep their ``A::f`` spelling.

Parsers are not thread-safe, so each thread gets its own (`threading.local`); the walk is
iterative and capped (`MAX_SYMBOLS`, `MAX_NODES`) so pathological files stay cheap.
"""

from __future__ import annotations

import ast
import functools
import importlib
import re
import threading
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import PurePosixPath
from typing import Any, Literal

SymbolKind = Literal["class", "function", "method", "interface", "type", "enum", "module", "other"]

MAX_SYMBOLS = 400  # per file
MAX_NODES = 250_000  # syntax nodes visited per file
MAX_SOURCE_CHARS = 1_000_000
MAX_ERROR_SHARE = 0.3  # more of the file inside ERROR nodes than this: no outline
SIGNATURE_CHARS = 200
DOC_CHARS = 200


@dataclass(frozen=True)
class Symbol:
    name: str  # "run"
    qualified: str  # "Retrier.run"
    kind: SymbolKind
    start: int  # 1-based, inclusive; decorators included
    end: int
    signature: str  # the definition's head, whitespace collapsed, e.g. "def run(self, fn):"
    doc: str | None  # first sentence of the docstring / doc comment


@dataclass(frozen=True)
class Outline:
    language: str | None  # "python", "typescript", ... (None: not a language we know)
    supported: bool  # an outliner exists for this language and is installed
    symbols: list[Symbol] = field(default_factory=list)  # source order
    parsed: bool = True  # False when the source could not be parsed well enough


# -- languages ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Grammar:
    language: str  # reported language name
    module: str  # python module of the grammar wheel
    function: str  # its function returning the language pointer
    dist: str  # distribution name, for the version


GRAMMARS: dict[str, _Grammar] = {
    "typescript": _Grammar(
        "typescript", "tree_sitter_typescript", "language_typescript", "tree-sitter-typescript"
    ),
    "tsx": _Grammar(
        "typescript", "tree_sitter_typescript", "language_tsx", "tree-sitter-typescript"
    ),
    "javascript": _Grammar(
        "javascript", "tree_sitter_javascript", "language", "tree-sitter-javascript"
    ),
    "go": _Grammar("go", "tree_sitter_go", "language", "tree-sitter-go"),
    "rust": _Grammar("rust", "tree_sitter_rust", "language", "tree-sitter-rust"),
    "java": _Grammar("java", "tree_sitter_java", "language", "tree-sitter-java"),
    "csharp": _Grammar("csharp", "tree_sitter_c_sharp", "language", "tree-sitter-c-sharp"),
    "ruby": _Grammar("ruby", "tree_sitter_ruby", "language", "tree-sitter-ruby"),
    "php": _Grammar("php", "tree_sitter_php", "language_php", "tree-sitter-php"),
    "c": _Grammar("c", "tree_sitter_c", "language", "tree-sitter-c"),
    "cpp": _Grammar("cpp", "tree_sitter_cpp", "language", "tree-sitter-cpp"),
}

EXTENSIONS: dict[str, str] = {  # file extension -> grammar id ("python" uses ast)
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
}

# Languages we can name but not outline (yet): reported with supported=False.
_KNOWN_ONLY = {
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".lua": "lua",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".m": "objective-c",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".clj": "clojure",
    ".vue": "vue",
    ".svelte": "svelte",
}


def grammar_for(path: str) -> str | None:
    """Grammar id for a path ("python" for Python), or None."""
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower())


def language_for(path: str) -> str | None:
    """Reported language name for a path, or None if unknown."""
    ext = PurePosixPath(path).suffix.lower()
    gid = EXTENSIONS.get(ext)
    if gid == "python":
        return "python"
    if gid:
        return GRAMMARS[gid].language
    return _KNOWN_ONLY.get(ext)


_LANG_LOCK = threading.Lock()
_LANGUAGES: dict[str, Any] = {}  # grammar id -> tree_sitter.Language (shareable)
_UNAVAILABLE: set[str] = set()
_LOCAL = threading.local()  # .parsers: grammar id -> tree_sitter.Parser (per thread)


def _language(gid: str) -> Any | None:
    if gid in _UNAVAILABLE:
        return None
    with _LANG_LOCK:
        if gid in _LANGUAGES:
            return _LANGUAGES[gid]
        try:
            import tree_sitter

            grammar = GRAMMARS[gid]
            module = importlib.import_module(grammar.module)
            lang = tree_sitter.Language(getattr(module, grammar.function)())
        except Exception:  # noqa: BLE001 - missing extra, ABI mismatch, ...: no outline
            _UNAVAILABLE.add(gid)
            return None
        _LANGUAGES[gid] = lang
        return lang


def _parser(gid: str) -> Any | None:
    parsers = getattr(_LOCAL, "parsers", None)
    if parsers is None:
        parsers = _LOCAL.parsers = {}
    if gid not in parsers:
        lang = _language(gid)
        if lang is None:
            return None
        import tree_sitter

        parsers[gid] = tree_sitter.Parser(lang)
    return parsers[gid]


@functools.lru_cache(maxsize=1)
def capabilities() -> dict[str, str]:
    """Grammar id -> grammar version for every grammar that loads here, plus the
    tree-sitter version ("tree-sitter"). Part of the index build fingerprint: installing or
    upgrading a grammar changes how files are chunked."""
    out: dict[str, str] = {}
    for gid, grammar in GRAMMARS.items():
        if _language(gid) is None:
            continue
        try:
            out[gid] = metadata.version(grammar.dist)
        except metadata.PackageNotFoundError:
            out[gid] = "?"
    if out:
        try:
            out["tree-sitter"] = metadata.version("tree-sitter")
        except metadata.PackageNotFoundError:
            out["tree-sitter"] = "?"
    return dict(sorted(out.items()))


def outline(path: str, source: str) -> Outline:
    """The symbols `source` (the contents of `path`) defines. Never raises."""
    gid = grammar_for(path)
    if gid is None:
        return Outline(language_for(path), supported=False)
    language = language_for(path)
    if len(source) > MAX_SOURCE_CHARS:
        return Outline(language, supported=True, parsed=False)
    try:
        if gid == "python":
            return _python_outline(source)
        if _has_lone_cr(source):  # tree-sitter would number lines differently
            return Outline(language, supported=_language(gid) is not None, parsed=False)
        parser = _parser(gid)
        if parser is None:
            return Outline(language, supported=False)
        return _ts_outline(gid, language, parser, source)
    except Exception:  # noqa: BLE001 - an outline is best effort
        return Outline(language, supported=True, parsed=False)


def _has_lone_cr(source: str) -> bool:
    return "\r" in source and source.count("\r") != source.count("\r\n")


# -- shared text helpers ---------------------------------------------------------------------

_WS = re.compile(r"\s+")
_SENTENCE = re.compile(r"^(.+?[.!?])(?:\s|$)", re.DOTALL)
_COMMENT_MARKERS = re.compile(r"^\s*(?:/\*\*?|\*/|\*(?!/)|///?|//!|#!?|--|<!--|-->)\s?")


def _collapse(text: str, limit: int) -> str:
    text = _WS.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _first_sentence(text: str | None) -> str | None:
    if not text:
        return None
    text = _WS.sub(" ", text).strip()
    if not text:
        return None
    m = _SENTENCE.match(text)
    sentence = m.group(1) if m else text
    return _collapse(sentence, DOC_CHARS)


_XML_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def _clean_comment(raw: str) -> str:
    lines = []
    for line in _XML_TAG.sub("", raw.replace("*/", "")).splitlines():
        line = _COMMENT_MARKERS.sub("", line, count=1).strip()
        if line.startswith("@"):  # JSDoc / Javadoc tags end the summary
            break
        lines.append(line)
    return " ".join(x for x in lines if x)


# -- python ------------------------------------------------------------------------------------


def _python_outline(source: str) -> Outline:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Outline("python", supported=True, parsed=False)
    lines = re.split(r"\r\n|\r|\n", source)
    symbols: list[Symbol] = []

    def head(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
        body_line = node.body[0].lineno if node.body else node.lineno
        last = node.lineno if body_line == node.lineno else body_line - 1
        text = "\n".join(lines[node.lineno - 1 : last])
        if body_line == node.lineno:  # one-liner: cut at the body
            text = text.split(":", 1)[0] + ":" if ":" in text else text
        return _collapse(text, SIGNATURE_CHARS)

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if len(symbols) >= MAX_SYMBOLS:
                return
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                is_class = isinstance(node, ast.ClassDef)
                kind: SymbolKind = "class" if is_class else ("method" if in_class else "function")
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                symbols.append(
                    Symbol(
                        name=node.name,
                        qualified=prefix + node.name,
                        kind=kind,
                        start=start,
                        end=node.end_lineno or node.lineno,
                        signature=head(node),
                        doc=_first_sentence(ast.get_docstring(node)),
                    )
                )
                if is_class:  # nested classes and methods; not functions inside functions
                    visit(node.body, f"{prefix}{node.name}.", True)
            elif isinstance(node, ast.If | ast.Try | ast.With | ast.AsyncWith) and not in_class:
                blocks = [getattr(node, f, []) for f in ("body", "orelse", "finalbody")]
                blocks += [h.body for h in getattr(node, "handlers", [])]
                for block in blocks:
                    visit(block, prefix, in_class)

    visit(tree.body, "", False)
    return Outline("python", supported=True, symbols=symbols)


# -- tree-sitter -------------------------------------------------------------------------------

# Per grammar: definition node type -> kind. A function kind becomes "method" inside a class.
_DEFS: dict[str, dict[str, SymbolKind]] = {
    "typescript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "method_definition": "method",
        "abstract_method_signature": "method",
        "internal_module": "module",
        "module": "module",
    },
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_spec": "type",  # refined to class / interface by its type
    },
    "rust": {
        "function_item": "function",
        "function_signature_item": "method",
        "struct_item": "class",
        "union_item": "class",
        "enum_item": "enum",
        "trait_item": "interface",
        "type_item": "type",
        "mod_item": "module",
    },
    "java": {
        "class_declaration": "class",
        "record_declaration": "class",
        "interface_declaration": "interface",
        "annotation_type_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "constructor_declaration": "method",
    },
    "csharp": {
        "class_declaration": "class",
        "struct_declaration": "class",
        "record_declaration": "class",
        "interface_declaration": "interface",
        "enum_declaration": "enum",
        "method_declaration": "method",
        "constructor_declaration": "method",
        "namespace_declaration": "module",
    },
    "ruby": {
        "class": "class",
        "module": "module",
        "method": "function",
        "singleton_method": "method",
    },
    "php": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "class",
        "enum_declaration": "enum",
        "function_definition": "function",
        "method_declaration": "method",
        "namespace_definition": "module",
    },
    "c": {
        "function_definition": "function",
        "struct_specifier": "class",
        "union_specifier": "class",
        "enum_specifier": "enum",
    },
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "class",
        "union_specifier": "class",
        "enum_specifier": "enum",
        "namespace_definition": "module",
    },
}
_DEFS["tsx"] = _DEFS["typescript"]

# kinds whose bodies hold further definitions (walked with the qualified name as prefix)
_CONTAINER_KINDS = {"class", "interface", "module", "enum"}
_FUNCTION_KINDS = {"function", "method"}
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_COMMENT_TYPES = {"comment", "line_comment", "block_comment", "doc_comment"}
# wrappers whose start belongs to the definition they hold (for ranges and doc comments)
_WRAPPERS = {"export_statement", "decorated_definition", "template_declaration"}


class _Walker:
    def __init__(self, gid: str, src: bytes):
        self.gid = gid
        self.src = src
        self.defs = _DEFS.get(gid, {})
        self.symbols: list[Symbol] = []
        self.error_bytes = 0
        self.visited = 0

    def text(self, node: Any) -> str:
        return self.src[node.start_byte : node.end_byte].decode("utf-8", "replace")

    # names ------------------------------------------------------------------------------
    def name_of(self, node: Any) -> str | None:
        t = node.type
        if t in ("function_definition",) and self.gid in ("c", "cpp"):
            return self._declarator_name(node.child_by_field_name("declarator"))
        if t == "field_definition":  # javascript class field
            prop = node.child_by_field_name("property")
            return self.text(prop) if prop is not None else None
        name = node.child_by_field_name("name")
        if name is None:
            return None
        return self.text(name).strip() or None

    def _declarator_name(self, node: Any) -> str | None:
        for _ in range(8):  # pointer_declarator -> function_declarator -> identifier ...
            if node is None:
                return None
            if node.type in (
                "identifier",
                "field_identifier",
                "qualified_identifier",
                "destructor_name",
                "operator_name",
                "type_identifier",
            ):
                return self.text(node)
            node = node.child_by_field_name("declarator")
        return None

    def go_receiver(self, node: Any) -> str | None:
        recv = node.child_by_field_name("receiver")
        if recv is None:
            return None
        stack = [recv]
        while stack:
            n = stack.pop()
            if n.type == "type_identifier":
                return self.text(n)
            stack.extend(reversed(n.named_children))
        return None

    # pieces -------------------------------------------------------------------------------
    def signature(self, node: Any, value: Any | None = None, start: Any | None = None) -> str:
        """The definition's head: from `start` (default `node`) to its body; without a body,
        its first line (continued while the line is visibly unfinished)."""
        first = start if start is not None else node
        body = (value or node).child_by_field_name("body")
        if body is not None and body.start_byte > first.start_byte:
            text = self.src[first.start_byte : body.start_byte].decode("utf-8", "replace")
        else:
            lines = (
                self.src[first.start_byte : node.end_byte].decode("utf-8", "replace").split("\n")
            )
            text = lines[0]
            for more in lines[1:3]:
                if not text.rstrip().endswith(("(", ",", "<", "=")):
                    break
                text += " " + more
        # a head can run into the comment above the first member (ruby): drop comment lines
        kept = [
            ln for ln in text.split("\n") if not ln.lstrip().startswith(("#", "//", "/*", "* "))
        ]
        text = "\n".join(kept) if kept else text
        return _collapse(text.rstrip().rstrip("{").rstrip(), SIGNATURE_CHARS)

    def doc(self, anchor: Any) -> str | None:
        """First sentence of the comment block directly above `anchor`."""
        parts: list[str] = []
        node, row = anchor.prev_sibling, anchor.start_point[0]
        if node is None and anchor.parent is not None and anchor.parent.type == "body_statement":
            node = anchor.parent.prev_sibling  # ruby: a comment before a class's first member
        while node is not None and node.type in _COMMENT_TYPES and node.end_point[0] >= row - 1:
            parts.append(self.text(node))
            row = node.start_point[0]
            node = node.prev_sibling
        if not parts:
            return None
        return _first_sentence(_clean_comment("\n".join(reversed(parts))))

    # walk -----------------------------------------------------------------------------------
    def walk(self, root: Any) -> None:
        # (node, qualified prefix, inside a class-like container)
        stack: list[tuple[Any, str, bool]] = [(root, "", False)]
        while stack:
            node, prefix, in_class = stack.pop()
            self.visited += 1
            if self.visited > MAX_NODES or len(self.symbols) >= MAX_SYMBOLS:
                return
            if node.type == "ERROR":
                self.error_bytes += node.end_byte - node.start_byte
                continue
            descend_prefix, descend_class, descend = prefix, in_class, True
            sym = self.definition(node, prefix, in_class)
            if sym is not None:
                self.symbols.append(sym)
                if sym.kind in _FUNCTION_KINDS:
                    descend = False  # nothing worth outlining inside function bodies
                else:
                    descend_prefix = sym.qualified + "."
                    descend_class = sym.kind in ("class", "interface", "enum")
            elif node.type == "impl_item":  # rust: methods of `impl Type` belong to Type
                typ = node.child_by_field_name("type")
                if typ is not None:
                    base = re.sub(r"<.*", "", self.text(typ)).strip()
                    descend_prefix, descend_class = f"{prefix}{base}.", True
            elif node.type in _FUNCTION_VALUES and self.gid in ("typescript", "tsx", "javascript"):
                # anonymous callbacks (describe(...), app.get(...)): not outlined, not entered
                descend = False
            if descend:
                for child in reversed(node.named_children):
                    stack.append((child, descend_prefix, descend_class))

    def definition(self, node: Any, prefix: str, in_class: bool) -> Symbol | None:
        t = node.type
        kind = self.defs.get(t)
        value = None
        anchor = node
        if kind is None:
            if self.gid in ("typescript", "tsx", "javascript"):
                if t == "variable_declarator":
                    value = node.child_by_field_name("value")
                    if value is None or value.type not in _FUNCTION_VALUES:
                        return None
                    kind = "function"
                    # the whole `const f = ...` statement (and its `export`)
                    if node.parent is not None and node.parent.type in (
                        "lexical_declaration",
                        "variable_declaration",
                    ):
                        anchor = node.parent
                elif t in ("public_field_definition", "field_definition") and in_class:
                    value = node.child_by_field_name("value")
                    if value is None or value.type not in _FUNCTION_VALUES:
                        return None
                    kind = "method"
                else:
                    return None
            else:
                return None
        if (
            self.gid in ("c", "cpp")
            and t in ("struct_specifier", "union_specifier", "enum_specifier")
            and node.child_by_field_name("body") is None
        ):
            return None  # a use of the type, not its definition
        name = self.name_of(node)
        if not name:
            return None
        if self.gid == "go":
            if t == "type_spec":
                typ = node.child_by_field_name("type")
                kind = {"struct_type": "class", "interface_type": "interface"}.get(
                    typ.type if typ is not None else "", "type"
                )
                parent = node.parent
                if (
                    parent is not None
                    and parent.type == "type_declaration"
                    and sum(1 for c in parent.named_children if c.type == "type_spec") == 1
                ):
                    anchor = parent  # `type X struct {...}`: the comment sits above `type`
            elif t == "method_declaration":
                recv = self.go_receiver(node)
                if recv:
                    prefix = f"{recv}."
        if kind == "function" and in_class:
            kind = "method"
        if self.gid == "ruby" and t == "method" and not in_class and prefix:
            kind = "method"  # methods of modules
        while anchor.parent is not None and anchor.parent.type in _WRAPPERS:
            anchor = anchor.parent
        qualified = name if "::" in name else prefix + name
        return Symbol(
            name=name.split("::")[-1],
            qualified=qualified,
            kind=kind,
            start=anchor.start_point[0] + 1,
            end=max(_last_line(anchor), _last_line(node)),
            signature=self.signature(
                node,
                value,
                anchor
                if value is not None or self.gid == "go" or anchor.type == "export_statement"
                else None,
            ),
            doc=self.doc(anchor),
        )


def _last_line(node: Any) -> int:
    """1-based last line of a node (a node ending right after a newline ends on the line
    before)."""
    row, col = node.end_point
    return row if col == 0 and row > node.start_point[0] else row + 1


def _ts_outline(gid: str, language: str | None, parser: Any, source: str) -> Outline:
    src = source.encode("utf-8")
    tree = parser.parse(src)
    root = tree.root_node
    walker = _Walker(gid, src)
    walker.walk(root)
    if src and walker.error_bytes / len(src) > MAX_ERROR_SHARE:
        return Outline(language, supported=True, parsed=False)
    symbols = sorted(walker.symbols, key=lambda s: (s.start, -s.end))
    return Outline(language, supported=True, symbols=symbols)
