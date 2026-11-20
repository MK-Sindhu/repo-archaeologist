"""Outlines (index/outline.py), outline-based chunking, the build fingerprint, and
`log_symbol` for outlined languages. Grammar tests skip when the "outline" extra is missing."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

from archaeologist.index import build as build_mod
from archaeologist.index import outline as outline_mod
from archaeologist.index.chunkers import chunk_file, chunk_outlined
from archaeologist.index.outline import Outline, capabilities, language_for, outline
from archaeologist.sources.git import GitRepo

needs_ts = pytest.mark.skipif(
    outline_mod._language("typescript") is None, reason="outline extra not installed"
)


def kinds(o: Outline) -> list[tuple[str, str, int, int]]:
    return [(s.kind, s.qualified, s.start, s.end) for s in o.symbols]


TS = """\
import { a } from "b";
const LIMIT = 3;

/** Options for the client. */
export interface Opts { retries: number }
export type Id = string;
enum Color { Red, Blue }

/**
 * A tiny HTTP client.
 * @public
 */
export class Client<T> extends Base {
  private x = 1;
  /** Send a request. Returns the response. */
  async send(req: Req): Promise<Res> {
    return this.do(req);
  }
  static create = () => new Client();
}

// Seconds to wait before retry `attempt`.
export function backoff(attempt: number): number {
  return Math.min(2 ** attempt, LIMIT);
}

export const handler = async (e: Event) => {
  return e;
};

describe("client", () => {
  function notOutlined() {}
});
"""


@needs_ts
def test_typescript_outline():
    o = outline("src/client.ts", TS)
    assert o.language == "typescript" and o.supported and o.parsed
    assert kinds(o) == [
        ("interface", "Opts", 5, 5),
        ("type", "Id", 6, 6),
        ("enum", "Color", 7, 7),
        ("class", "Client", 13, 20),
        ("method", "Client.send", 16, 18),
        ("method", "Client.create", 19, 19),
        ("function", "backoff", 23, 25),
        ("function", "handler", 27, 29),
    ]
    by_name = {s.qualified: s for s in o.symbols}
    assert by_name["Opts"].doc == "Options for the client."
    assert by_name["Client"].doc == "A tiny HTTP client."  # stops at the @public tag
    assert by_name["Client"].signature == "export class Client<T> extends Base"
    assert by_name["Client.send"].signature == "async send(req: Req): Promise<Res>"
    assert by_name["Client.send"].doc == "Send a request."
    assert by_name["backoff"].doc == "Seconds to wait before retry `attempt`."
    assert by_name["handler"].signature == "export const handler = async (e: Event) =>"


@needs_ts
def test_tsx_and_javascript():
    tsx = "export function App(): JSX.Element {\n  return <div>hi</div>;\n}\n"
    assert kinds(outline("ui/App.tsx", tsx)) == [("function", "App", 1, 3)]
    js = (
        "export default function main() {}\n"
        "class Old extends React.Component {\n  render() { return null }\n  onClick = () => {}\n}\n"
        "const util = function () {}\n"
    )
    o = outline("a.jsx", js)
    assert o.language == "javascript"
    assert kinds(o) == [
        ("function", "main", 1, 1),
        ("class", "Old", 2, 5),
        ("method", "Old.render", 3, 3),
        ("method", "Old.onClick", 4, 4),
        ("function", "util", 6, 6),
    ]


@pytest.mark.skipif(outline_mod._language("go") is None, reason="outline extra not installed")
def test_go_outline():
    src = (
        "package main\n\n// Server serves things.\ntype Server struct {\n\tport int\n}\n\n"
        "type Handler interface { Serve() }\n\n// Start listens on the port.\n"
        "func (s *Server) Start() error {\n\treturn nil\n}\n\nfunc main() {}\n"
    )
    o = outline("cmd/main.go", src)
    assert kinds(o) == [
        ("class", "Server", 4, 6),
        ("interface", "Handler", 8, 8),
        ("method", "Server.Start", 11, 13),
        ("function", "main", 15, 15),
    ]
    assert o.symbols[0].doc == "Server serves things."
    assert o.symbols[2].doc == "Start listens on the port."


@pytest.mark.skipif(outline_mod._language("rust") is None, reason="outline extra not installed")
def test_rust_outline():
    src = (
        "/// A point.\nstruct Point { x: i32 }\n\ntrait Shape {\n    fn area(&self) -> f64;\n}\n\n"
        "impl Shape for Point {\n    /// Always zero.\n    fn area(&self) -> f64 { 0.0 }\n}\n\n"
        "mod util {\n    pub fn helper() {}\n}\n"
    )
    assert kinds(outline("src/lib.rs", src)) == [
        ("class", "Point", 2, 2),
        ("interface", "Shape", 4, 6),
        ("method", "Shape.area", 5, 5),
        ("method", "Point.area", 10, 10),
        ("module", "util", 13, 15),
        ("function", "util.helper", 14, 14),
    ]


@pytest.mark.skipif(outline_mod._language("java") is None, reason="outline extra not installed")
def test_java_outline():
    src = (
        "package x;\n\n/** Thing doer. */\npublic class Doer {\n  /** Make one. */\n  public Doer() {}\n"
        "  public int run(int a) { return a; }\n  interface Inner { void go(); }\n}\n"
    )
    o = outline("src/Doer.java", src)
    assert kinds(o) == [
        ("class", "Doer", 4, 9),
        ("method", "Doer.Doer", 6, 6),
        ("method", "Doer.run", 7, 7),
        ("interface", "Doer.Inner", 8, 8),
        ("method", "Doer.Inner.go", 8, 8),
    ]
    assert o.symbols[0].doc == "Thing doer."


def test_python_outline_matches_chunker_naming():
    src = (
        'class Retrier:\n    """Retries calls."""\n\n    @staticmethod\n    def build():\n'
        "        pass\n\n    def run(self, fn):\n        return fn()\n\n\n"
        "def backoff(attempt): return 2 ** attempt\n"
    )
    o = outline("src/pkg/retry.py", src)
    assert o.language == "python" and o.supported
    assert kinds(o) == [
        ("class", "Retrier", 1, 9),
        ("method", "Retrier.build", 4, 6),
        ("method", "Retrier.run", 8, 9),
        ("function", "backoff", 12, 12),
    ]
    assert o.symbols[0].doc == "Retries calls."
    assert o.symbols[3].signature == "def backoff(attempt):"
    symbols = {c.symbol for c in chunk_file("src/pkg/retry.py", src) if c.symbol}
    assert symbols == {s.qualified for s in o.symbols}


def test_fallbacks_never_raise():
    assert outline("notes.xyz", "anything") == Outline(None, supported=False)
    assert outline("App.kt", "fun main() {}") == Outline("kotlin", supported=False)
    assert outline("bad.py", "def (:\n") == Outline("python", supported=True, parsed=False)
    assert outline("huge.py", "x = 1\n" * 200_000).parsed is False
    lone_cr = outline("a.ts", "function a() {}\rfunction b() {}\r")
    assert lone_cr.symbols == [] and not lone_cr.parsed


@needs_ts
def test_mostly_errors_gives_no_outline():
    garbage = "}}}} {{{{ ((( ;;; class class ))) => => " * 50
    o = outline("junk.ts", garbage)
    assert o.symbols == [] and not o.parsed


def test_missing_grammar_falls_back(monkeypatch):
    monkeypatch.setattr(outline_mod, "_parser", lambda gid: None)
    assert outline("a.go", "package a\nfunc A() {}\n") == Outline("go", supported=False)
    chunks = chunk_file("a.go", "package a\nfunc A() {}\n")
    assert [c.meta["node"] for c in chunks] == ["window"]


@needs_ts
def test_outline_is_thread_safe():
    expected = kinds(outline("src/client.ts", TS))
    results: list[list] = []
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(15):
                results.append(kinds(outline("src/client.ts", TS)))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 120 and all(r == expected for r in results)


@needs_ts
def test_capabilities_list_installed_grammars():
    caps = capabilities()
    for gid in ("typescript", "tsx", "javascript", "go", "rust", "java", "csharp", "ruby", "php", "c", "cpp"):
        assert caps.get(gid), gid
    assert caps["tree-sitter"]
    assert language_for("x.tsx") == "typescript" and language_for("x.cs") == "csharp"


# -- chunking -------------------------------------------------------------------------------


@needs_ts
def test_typescript_file_becomes_symbol_chunks():
    chunks = chunk_file("src/client.ts", TS)
    by_symbol = {c.symbol: c for c in chunks if c.symbol}
    assert set(by_symbol) == {"Opts", "Id", "Color", "Client", "Client.send", "Client.create", "backoff", "handler"}
    send = by_symbol["Client.send"]
    assert send.id == "code:src/client.ts#L15-L18"  # includes its doc comment
    assert send.title == "Client.send in src/client.ts" and send.meta["node"] == "method"
    assert send.text.startswith("src/client.ts:15-18 Client.send\n  /** Send a request.")
    klass = by_symbol["Client"]
    assert klass.title == "class Client in src/client.ts" and klass.meta["node"] == "class"
    assert klass.id == "code:src/client.ts#L9-L20"
    assert "async send(req: Req): Promise<Res>" in klass.text and "return this.do" not in klass.text
    backoff = by_symbol["backoff"]
    assert backoff.id == "code:src/client.ts#L22-L25"  # with the // comment above
    modules = [c for c in chunks if c.meta["node"] == "module"]
    assert modules and modules[0].start_line == 1
    assert 'import { a } from "b";' in modules[0].text and "describe(" in modules[-1].text
    # every line of the file is in some chunk
    covered = {ln for c in chunks for ln in range(c.start_line, c.end_line + 1)}
    nonblank = {i + 1 for i, line in enumerate(TS.splitlines()) if line.strip()}
    assert nonblank <= covered


def test_long_definitions_are_windowed():
    from archaeologist.index.outline import Symbol

    src = "\n".join(["function big() {", *[f"  x{i}();" for i in range(300)], "}"]) + "\n"
    sym = Symbol("big", "big", "function", 1, 302, "function big()", None)
    chunks = chunk_outlined("big.ts", src, [sym], max_lines=200)
    assert len(chunks) > 1 and all(c.symbol == "big" for c in chunks)
    assert {c.meta.get("parts") for c in chunks} == {len(chunks)}


def test_fingerprint_follows_grammar_capabilities(monkeypatch):
    args = ((".ts",), (), 200_000, 5000)
    monkeypatch.setattr(build_mod, "outline_capabilities", lambda: {"typescript": "0.23.2"})
    a = build_mod._fingerprint(*args)
    monkeypatch.setattr(build_mod, "outline_capabilities", lambda: {"typescript": "0.23.3"})
    b = build_mod._fingerprint(*args)
    monkeypatch.setattr(build_mod, "outline_capabilities", dict)
    c = build_mod._fingerprint(*args)
    assert len({a, b, c}) == 3
    assert build_mod.CHUNKER_VERSION == "2"


# -- history of an outlined symbol ------------------------------------------------------------

V1 = "export function alpha() {\n  return 1;\n}\n\nexport function beta() {\n  return 2;\n}\n"
V2 = V1.replace("return 2;", "return 22;")
V3 = V2.replace("return 1;", "return 11;")
V4 = (
    V3
    + "\nclass A {\n  run() {\n    return 'a';\n  }\n}\n\nclass B {\n  run() {\n    return 'b';\n  }\n}\n"
)
V5 = V4.replace("return 'b';", "return 'bb';")


def _git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Dev",
        "GIT_AUTHOR_EMAIL": "dev@example.com",
        "GIT_COMMITTER_NAME": "Dev",
        "GIT_COMMITTER_EMAIL": "dev@example.com",
    }
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd, env=env, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def ts_repo(tmp_path: Path) -> tuple[Path, list[str]]:
    root = tmp_path / "tsrepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    shas = []
    for i, (msg, text) in enumerate(
        [("add alpha and beta", V1), ("tweak beta", V2), ("tweak alpha", V3), ("add A and B", V4), ("tweak B.run", V5)]
    ):
        (root / "src").mkdir(exist_ok=True)
        (root / "src/lib.ts").write_text(text)
        _git(root, "add", "src/lib.ts")
        date = f"2024-0{i + 1}-15T12:00:00+00:00"
        _git(root, "-c", "user.name=Dev", "commit", "-q", "-m", msg, f"--date={date}")
        shas.append(_git(root, "rev-parse", "HEAD"))
    return root, shas


@needs_ts
def test_log_symbol_traces_a_typescript_function(ts_repo):
    root, (c1, c2, c3, _, _) = ts_repo
    repo = GitRepo(root)
    assert [c.sha for c in repo.log_symbol("src/lib.ts", "beta")] == [c2, c1]
    assert [c.sha for c in repo.log_symbol("src/lib.ts", "alpha")] == [c3, c1]


@needs_ts
def test_log_symbol_picks_the_definition_near_the_lines(ts_repo):
    root, (_, _, _, c4, c5) = ts_repo
    repo = GitRepo(root)
    o = outline("src/lib.ts", V5)
    a_run = next(s for s in o.symbols if s.qualified == "A.run")
    b_run = next(s for s in o.symbols if s.qualified == "B.run")
    assert [c.sha for c in repo.log_symbol("src/lib.ts", "run", near=(b_run.start, b_run.end))] == [c5, c4]
    assert [c.sha for c in repo.log_symbol("src/lib.ts", "run", near=(a_run.start, a_run.end))] == [c4]
    assert [c.sha for c in repo.log_symbol("src/lib.ts", "B.run")] == [c5, c4]
