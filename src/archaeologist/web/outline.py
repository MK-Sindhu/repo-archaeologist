"""File outlines for the Map tab (contract: `OutlineResponse`).

Uses `archaeologist.index.outline.outline(path, source)` (tree-sitter + Python ast) when it
exists; otherwise a private Python-ast outline, so the endpoint works either way.
"""

from __future__ import annotations

import ast
import logging
import re
import warnings
from typing import Any

from .survey import language_of

log = logging.getLogger(__name__)

KINDS = {"class", "function", "method", "interface", "type", "enum", "module", "other"}
MAX_SYMBOLS = 500
_SENTENCE = re.compile(r"(.+?[.!?])(?:\s|$)", re.DOTALL)


def file_outline(path: str, source: str) -> dict[str, Any]:
    """{language, supported, symbols} for one file's text."""
    try:
        from ..index.outline import outline as index_outline  # builder B's tree-sitter outline
    except ImportError:
        index_outline = None
    if index_outline is not None:
        try:
            result = index_outline(path, source)
            symbols = [_symbol(s) for s in list(getattr(result, "symbols", []) or [])[:MAX_SYMBOLS]]
            return {
                "language": getattr(result, "language", None) or _language(path),
                "supported": bool(getattr(result, "supported", bool(symbols))),
                "symbols": [s for s in symbols if s is not None],
            }
        except Exception:
            log.debug("outline failed for %s", path, exc_info=True)
    if path.endswith((".py", ".pyi")):
        symbols = python_outline(source)
        return {"language": "python", "supported": symbols is not None, "symbols": symbols or []}
    return {"language": _language(path), "supported": False, "symbols": []}


def _language(path: str) -> str | None:
    name = language_of(path)
    return name.lower().replace(" ", "-") if name else None


def _symbol(s: Any) -> dict[str, Any] | None:
    get = (lambda k, d=None: s.get(k, d)) if isinstance(s, dict) else (lambda k, d=None: getattr(s, k, d))
    try:
        start, end = int(get("start")), int(get("end"))
    except (TypeError, ValueError):
        return None
    name = str(get("name") or "")
    kind = str(get("kind") or "other")
    return {
        "name": name,
        "qualified": str(get("qualified") or name),
        "kind": kind if kind in KINDS else "other",
        "start": start,
        "end": max(start, end),
        "signature": str(get("signature") or ""),
        "doc": get("doc") or None,
    }


def first_sentence(doc: str | None) -> str | None:
    if not doc:
        return None
    text = " ".join(doc.strip().split())
    m = _SENTENCE.match(text)
    out = (m.group(1) if m else text)[:240]
    return out or None


def python_outline(source: str) -> list[dict[str, Any]] | None:
    """Classes, functions and methods (one class level deep for methods), in source order;
    None when the source does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    lines = source.splitlines()
    out: list[dict[str, Any]] = []

    def signature(node: ast.AST) -> str:
        line = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
        return line[:200]

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for node in body:
            if len(out) >= MAX_SYMBOLS:
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append({
                    "name": node.name,
                    "qualified": f"{prefix}{node.name}",
                    "kind": "method" if in_class else "function",
                    "start": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                    "signature": signature(node),
                    "doc": first_sentence(ast.get_docstring(node)),
                })
            elif isinstance(node, ast.ClassDef):
                out.append({
                    "name": node.name,
                    "qualified": f"{prefix}{node.name}",
                    "kind": "class",
                    "start": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                    "signature": signature(node),
                    "doc": first_sentence(ast.get_docstring(node)),
                })
                visit(node.body, f"{prefix}{node.name}.", True)

    visit(tree.body, "", False)
    return out
