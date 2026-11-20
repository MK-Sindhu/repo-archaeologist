"""Tests for archaeologist.sources.git against real (temporary) git repositories."""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from archaeologist.sources.git import BlameHunk, CommitInfo, GitRepo, parse_github_remote

RETRY = "src/pkg/retry.py"

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Dev",
    "GIT_AUTHOR_EMAIL": "dev@example.com",
    "GIT_COMMITTER_NAME": "Dev",
    "GIT_COMMITTER_EMAIL": "dev@example.com",
}

Files = dict[str, str | bytes | None]  # path -> content; None deletes the path


def _git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = {**_ENV, **({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {})}
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init(root: Path, remotes: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    for name, url in (remotes or {}).items():
        _git(root, "remote", "add", name, url)
    return root


def _commit(root: Path, message: str, files: Files) -> str:
    for rel, content in files.items():
        if content is None:
            _git(root, "rm", "-q", "--", rel)
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content)
        _git(root, "add", "--", rel)
    n = int(_git(root, "rev-list", "--count", "--all"))  # distinct, increasing commit dates
    _git(
        root, "commit", "-q", "--allow-empty", "-m", message, date=f"2024-06-{n + 1:02d}T00:00:00Z"
    )
    return _git(root, "rev-parse", "HEAD")


def _make_repo(root: Path, commits: list[tuple[str, Files]]) -> tuple[GitRepo, list[str]]:
    _init(root)
    shas = [_commit(root, message, files) for message, files in commits]
    return GitRepo(root), shas


@pytest.fixture
def toy(toy_repo_shas) -> tuple[GitRepo, list[str]]:
    path, shas = toy_repo_shas
    return GitRepo(path), shas


def _shas(commits: list[CommitInfo]) -> list[str]:
    return [c.sha for c in commits]


# ---------------------------------------------------------------------- construction


def test_constructor_rejects_non_repositories(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ValueError):
        GitRepo(plain)
    with pytest.raises(ValueError):
        GitRepo(tmp_path / "does-not-exist")


def test_constructor_finds_toplevel_from_subdirectory(toy_repo):
    assert GitRepo(toy_repo / "src" / "pkg").root == GitRepo(toy_repo).root


# ---------------------------------------------------------------------- HEAD, files, contents


def test_head_and_ls_files(toy):
    repo, shas = toy
    assert repo.head_sha() == shas[3]
    assert repo.ls_files() == ["README.md", RETRY]


def test_read_file_at_revisions_and_path_forms(toy):
    repo, shas = toy
    head = (repo.root / RETRY).read_text()
    assert repo.read_file(RETRY) == head
    assert repo.read_file("./" + RETRY) == head
    assert repo.read_file(repo.root / RETRY) == head
    assert repo.read_file("/" + RETRY) == head  # model-style "absolute" repo path
    v1 = repo.read_file(RETRY, rev=shas[0])
    assert v1 is not None and "return 2 ** attempt" in v1 and "MAX_BACKOFF" not in v1
    assert repo.read_file(RETRY, rev=shas[0][:10]) == v1


@pytest.mark.parametrize(
    ("path", "rev"),
    [
        ("missing.py", "HEAD"),
        ("src", "HEAD"),  # a directory, not a file
        ("../outside.py", "HEAD"),
        (RETRY, "no-such-branch"),
        (RETRY, "--output=/tmp/pwned"),
        (RETRY, ""),
    ],
)
def test_read_file_fails_soft(toy, path, rev):
    repo, _ = toy
    assert repo.read_file(path, rev=rev) is None


# ---------------------------------------------------------------------- iter_commits / show


def test_iter_commits_newest_first_with_metadata(toy):
    repo, shas = toy
    commits = list(repo.iter_commits())
    assert _shas(commits) == shas[::-1]
    c2 = commits[2]
    assert c2.subject == "Cap backoff at 30s to avoid huge sleeps (#12)"
    assert c2.body == "Fixes #7. Users reported multi-minute sleeps."
    assert c2.files == [RETRY]
    assert c2.author == "Dev"
    assert c2.date == "2024-02-15T12:00:00+00:00"
    assert shas[1].startswith(c2.short_sha) and len(c2.short_sha) >= 7
    assert c2.diff is None
    assert commits[3].files == ["README.md", RETRY]


def test_iter_commits_limits_and_paths(toy):
    repo, shas = toy
    assert _shas(repo.iter_commits(max_count=2)) == [shas[3], shas[2]]
    assert _shas(repo.iter_commits(max_count=0)) == []
    assert _shas(repo.iter_commits(paths=["README.md"])) == [shas[3], shas[0]]
    assert _shas(repo.iter_commits(paths=["src"])) == [shas[2], shas[1], shas[0]]
    assert list(repo.iter_commits(paths=["missing.py"])) == []


def test_iter_commits_can_stop_early(toy):
    repo, shas = toy
    gen = repo.iter_commits()
    assert next(gen).sha == shas[3]
    gen.close()  # kills the streaming git process without error


def test_show_includes_diff(toy):
    repo, shas = toy
    info = repo.show(shas[1])
    assert info is not None and info.sha == shas[1]
    assert info.files == [RETRY]
    assert info.diff is not None and "+MAX_BACKOFF = 30" in info.diff
    assert info.diff.startswith("diff --git a/src/pkg/retry.py b/src/pkg/retry.py")
    assert repo.show(shas[1][:8]).sha == shas[1]
    assert repo.show("HEAD").sha == shas[3]
    assert repo.show(shas[1], max_diff_chars=0).diff is None


def test_show_truncates_diff(toy):
    repo, shas = toy
    full = repo.show(shas[2]).diff
    short = repo.show(shas[2], max_diff_chars=120).diff
    assert len(full) > 120
    assert short.endswith("[diff truncated]")
    assert len(short) <= 120 + len("\n… [diff truncated]")
    assert full.startswith(short.removesuffix("\n… [diff truncated]"))


def test_show_bounds_memory_on_huge_diffs(tmp_path):
    big = "".join(f"line {i}\n" for i in range(200_000))
    repo, shas = _make_repo(tmp_path / "big", [("vendor a huge file", {"big.txt": big})])
    info = repo.show(shas[0], max_diff_chars=1000)
    assert info is not None and info.files == ["big.txt"]
    assert len(info.diff) <= 1000 + len("\n… [diff truncated]")
    assert info.diff.endswith("[diff truncated]")


@pytest.mark.parametrize(
    "rev", ["deadbeefdeadbeef", "no-such-branch", "--help", "", "HEAD:README.md"]
)
def test_show_unknown_revision_is_none(toy, rev):
    repo, _ = toy
    assert repo.show(rev) is None


# ---------------------------------------------------------------------- blame


def test_blame_backoff_body(toy):
    repo, shas = toy
    hunks = repo.blame(RETRY, 8, 13)
    assert [(h.sha, h.start, h.end) for h in hunks] == [
        (shas[2], 8, 8),  # def backoff(attempt, jitter=True):  -- jitter commit
        (shas[0], 9, 9),  # docstring                           -- original helper
        (shas[2], 10, 13),  # delay / jitter / return           -- jitter commit
    ]
    assert hunks[0] == BlameHunk(
        sha=shas[2],
        start=8,
        end=8,
        author="Dev",
        date="2024-03-15T12:00:00+00:00",
        summary="Add jitter to backoff (#15)",
    )
    assert hunks[1].summary == "Add retry helper"


def test_blame_at_older_revision_and_clamping(toy):
    repo, shas = toy
    at_c2 = repo.blame(RETRY, 6, 8, rev=shas[1])
    assert [(h.sha, h.start, h.end) for h in at_c2] == [(shas[0], 6, 7), (shas[1], 8, 8)]
    tail = repo.blame(RETRY, 20, 10_000)
    assert tail[-1].end == 26  # clamped to the file's 26 lines
    assert repo.blame(RETRY, 0, 1)[0].start == 1


@pytest.mark.parametrize(
    ("path", "start", "end", "rev"),
    [
        ("missing.py", 1, 5, "HEAD"),
        (RETRY, 100, 120, "HEAD"),
        (RETRY, 5, 2, "HEAD"),
        (RETRY, 1, 5, "no-such-rev"),
    ],
)
def test_blame_fails_soft(toy, path, start, end, rev):
    repo, _ = toy
    assert repo.blame(path, start, end, rev=rev) == []


def test_blame_honours_ignore_revs_file(tmp_path):
    root = _init(tmp_path / "fmt")
    c1 = _commit(root, "add settings", {"a.py": "x = 'a'\ny = 1\n"})
    c2 = _commit(root, "reformat quotes", {"a.py": 'x = "a"\ny = 1\n'})
    _commit(root, "ignore formatting commit", {".git-blame-ignore-revs": f"{c2}\n"})
    assert GitRepo(root).blame("a.py", 1, 1)[0].sha == c1


# ---------------------------------------------------------------------- log -L


def test_log_symbol_backoff_history(toy):
    repo, shas = toy
    commits = repo.log_symbol(RETRY, "backoff")
    assert _shas(commits) == [shas[2], shas[1], shas[0]]
    assert all(c.files == [RETRY] or RETRY in c.files for c in commits)
    assert "+    if jitter:" in commits[0].diff
    assert commits[1].subject.startswith("Cap backoff")
    assert _shas(repo.log_symbol(RETRY, "backoff", max_count=2)) == [shas[2], shas[1]]
    assert _shas(repo.log_symbol(RETRY, "backoff()")) == _shas(commits)


@pytest.mark.parametrize("symbol", ["Retrier.run", "run", "pkg.retry.Retrier.run"])
def test_log_symbol_method_introduced_once(toy, symbol):
    repo, shas = toy
    # plain `git log -L` would also report c2 and c1 (the replaced `return` line)
    assert _shas(repo.log_symbol(RETRY, symbol)) == [shas[2]]


def test_log_symbol_module_constant(toy):
    repo, shas = toy
    assert _shas(repo.log_symbol(RETRY, "MAX_BACKOFF")) == [shas[1]]


def test_log_symbol_non_python_uses_funcname(toy):
    repo, shas = toy
    # markdown funcname driver: the "## Retries" section
    assert _shas(repo.log_symbol("README.md", "Retries")) == [shas[3], shas[0]]


def test_log_symbol_falls_back_to_path_history(toy):
    repo, shas = toy
    assert _shas(repo.log_symbol(RETRY, "no_such_symbol")) == [shas[2], shas[1], shas[0]]
    assert repo.log_symbol("missing.py", "backoff") == []
    assert repo.log_symbol(RETRY, "") == []


def test_log_symbol_unparseable_python_uses_python_funcname_driver(tmp_path):
    v1 = (  # Python 2 source: ast cannot parse it
        'class Foo:\n    def bar(self):\n        print "one"\n\n    def baz(self):\n        return 1\n'
    )
    v2 = v1.replace('"one"', '"two"')
    v3 = v2.replace("return 1", "return 2")
    repo, shas = _make_repo(
        tmp_path / "py2",
        [("add", {"legacy.py": v1}), ("bar", {"legacy.py": v2}), ("baz", {"legacy.py": v3})],
    )
    # the indented method is only a funcname line with git's python driver
    assert _shas(repo.log_symbol("legacy.py", "Foo.bar")) == [shas[1], shas[0]]


def test_log_symbol_follows_file_and_symbol_renames(tmp_path):
    repo, shas = _make_repo(
        tmp_path / "ren",
        [
            ("add helper", {"mod.py": "def old_name(x):\n    return x + 1\n"}),
            ("tweak helper", {"mod.py": "def old_name(x):\n    return x + 2\n"}),
            ("rename helper", {"mod.py": "def new_name(x):\n    return x + 2\n"}),
            ("move module", {"mod.py": None, "lib.py": "def new_name(x):\n    return x + 2\n"}),
        ],
    )
    assert _shas(repo.log_symbol("lib.py", "new_name")) == [shas[2], shas[1], shas[0]]
    assert _shas(repo.iter_commits(paths=["lib.py"])) == shas[::-1]


def test_log_lines(toy):
    repo, shas = toy
    commits = repo.log_lines(RETRY, 5, 5)  # MAX_BACKOFF = 30
    assert _shas(commits) == [shas[1]]
    assert "+MAX_BACKOFF = 30" in commits[0].diff
    assert commits[0].subject.startswith("Cap backoff") and commits[0].files == [RETRY]
    assert _shas(repo.log_lines(RETRY, 1, 1)) == [shas[0]]
    assert repo.log_lines(RETRY, 26, 10_000)[0].sha == shas[2]  # clamped to the file
    assert repo.log_lines(RETRY, 5, 5, max_diff_chars=0)[0].diff is None
    assert repo.log_lines(RETRY, 100, 200) == []
    assert repo.log_lines("missing.py", 1, 3) == []


# ---------------------------------------------------------------------- pickaxe


def test_pickaxe(toy):
    repo, shas = toy
    # c2 adds the constant to retry.py; c4 mentions it in README.md
    assert _shas(repo.pickaxe("MAX_BACKOFF")) == [shas[3], shas[1]]
    assert _shas(repo.pickaxe("MAX_BACKOFF", paths=[RETRY])) == [shas[1]]
    assert _shas(repo.pickaxe("MAX_BACKOFF", max_count=1)) == [shas[3]]
    assert repo.pickaxe("") == []
    assert repo.pickaxe("term_that_never_existed") == []


# ---------------------------------------------------------------------- remotes


def test_remote_slug_toy(toy):
    repo, _ = toy
    assert repo.remote_slug() == ("acme", "toy")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/toy.git",
        "https://github.com/acme/toy",
        "https://github.com/acme/toy/",
        "https://user:token@github.com/acme/toy.git",
        "http://www.github.com/acme/toy",
        "git@github.com:acme/toy.git",
        "git@github.com:acme/toy",
        "ssh://git@github.com/acme/toy.git",
        "ssh://git@ssh.github.com:443/acme/toy.git",
        "git://github.com/acme/toy.git",
    ],
)
def test_parse_github_remote(url):
    assert parse_github_remote(url) == ("acme", "toy")


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/toy.git",
        "git@bitbucket.org:acme/toy.git",
        "https://github.com/acme",
        "/srv/git/toy.git",
        "",
    ],
)
def test_parse_github_remote_rejects_other_urls(url):
    assert parse_github_remote(url) is None


def test_parse_github_remote_keeps_dotted_names():
    assert parse_github_remote("git@github.com:acme/acme.github.io.git") == (
        "acme",
        "acme.github.io",
    )


def test_remote_slug_ssh_and_remote_preference(tmp_path):
    ssh = _init(tmp_path / "ssh", {"origin": "git@github.com:acme/toy.git"})
    assert GitRepo(ssh).remote_slug() == ("acme", "toy")
    fork = _init(
        tmp_path / "fork",
        {"origin": "https://gitlab.com/me/toy.git", "upstream": "https://github.com/acme/toy.git"},
    )
    assert GitRepo(fork).remote_slug() == ("acme", "toy")
    assert GitRepo(_init(tmp_path / "none")).remote_slug() is None


# ---------------------------------------------------------------------- evidence


def test_to_evidence_c2(toy):
    repo, shas = toy
    c2 = next(c for c in repo.iter_commits() if c.sha == shas[1])
    e = c2.to_evidence("acme", "toy")
    assert e.id == f"commit:{shas[1]}"
    assert e.kind == "commit"
    assert e.title == "Cap backoff at 30s to avoid huge sleeps (#12)"
    assert e.refs == ["pr:12", "issue:7"]
    assert e.url == f"https://github.com/acme/toy/commit/{shas[1]}"
    assert e.source == "git"
    assert e.date == "2024-02-15T12:00:00+00:00"
    assert "Fixes #7. Users reported multi-minute sleeps." in e.text
    assert RETRY in e.text and "Diff:" not in e.text
    assert c2.to_evidence().url is None


def test_to_evidence_with_diff_and_other_refs(toy):
    repo, shas = toy
    e = repo.show(shas[2]).to_evidence("acme", "toy")
    assert e.refs == ["pr:15", "gh:9"]
    assert "Diff:\n" in e.text and "random.uniform" in e.text


def test_to_evidence_caps_file_list():
    info = CommitInfo(
        sha="a" * 40,
        short_sha="aaaaaaa",
        author="Dev",
        date="2024-01-01T00:00:00+00:00",
        subject="",
        files=[f"f{i}.txt" for i in range(60)],
    )
    e = info.to_evidence()
    assert "f49.txt" in e.text and "f50.txt" not in e.text and "(+10 more)" in e.text
    assert e.title == "commit aaaaaaa" and e.refs == []


# ---------------------------------------------------------------------- awkward repositories


def test_empty_repository(tmp_path):
    repo = GitRepo(_init(tmp_path / "empty"))
    assert repo.head_sha() is None
    assert repo.ls_files() == []
    assert list(repo.iter_commits()) == []
    assert repo.show("HEAD") is None
    assert repo.read_file("x.py") is None
    assert repo.blame("x.py", 1, 2) == []
    assert repo.log_lines("x.py", 1, 2) == []
    assert repo.log_symbol("x.py", "f") == []
    assert repo.pickaxe("x") == []
    assert repo.remote_slug() is None


def test_binary_files(tmp_path):
    blob = b"\x89PNG\r\n\x1a\n\x00\x00binary" * 10
    repo, shas = _make_repo(tmp_path / "bin", [("add logo", {"logo.png": blob, "a.txt": "a\n"})])
    assert repo.ls_files() == ["a.txt", "logo.png"]
    assert repo.read_file("logo.png") is None
    assert repo.blame("logo.png", 1, 1) == []
    info = repo.show(shas[0])
    assert info.files == ["a.txt", "logo.png"]
    assert "Binary files /dev/null and b/logo.png differ" in info.diff


def test_unusual_file_names(tmp_path):
    name = 'docs/a "quoted"\tname é.txt'
    repo, shas = _make_repo(tmp_path / "names", [("odd name", {name: "hello\n"})])
    assert repo.ls_files() == [name]
    assert next(repo.iter_commits()).files == [name]
    assert repo.read_file(name) == "hello\n"
    assert repo.blame(name, 1, 1)[0].sha == shas[0]
    assert _shas(repo.log_lines(name, 1, 1)) == [shas[0]]


def test_merge_commit_files_are_relative_to_first_parent(tmp_path):
    repo, _ = _make_repo(tmp_path / "merge", [("base", {"a.txt": "a\n"})])
    root = repo.root
    _git(root, "checkout", "-q", "-b", "feature")
    _commit(root, "feature work", {"b.txt": "b\n"})
    _git(root, "checkout", "-q", "main")
    _commit(root, "main work", {"c.txt": "c\n"})
    _git(root, "merge", "-q", "--no-ff", "-m", "Merge pull request #5 from acme/feature", "feature")
    merge = next(repo.iter_commits())
    assert merge.subject.startswith("Merge pull request #5")
    assert merge.files == ["b.txt"]
    assert merge.to_evidence("acme", "toy").refs == ["gh:5"]
    assert "+b" in repo.show(merge.sha).diff


def test_old_python_sources_do_not_emit_syntax_warnings():
    code = (
        "from archaeologist.sources.git import _python_symbols\n"
        "print(sorted(_python_symbols('import re\\nPAT = re.compile(\"\\\\\\\\d+\")\\n')))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-W", "default", "-c", code], capture_output=True, text=True, check=True
    )
    assert proc.stdout.strip() == "['PAT']"
    assert "SyntaxWarning" not in proc.stderr


# ---------------------------------------------------------------------- concurrency


def test_concurrent_calls_are_safe(toy):
    repo, shas = toy
    calls = [
        lambda: _shas(repo.log_symbol(RETRY, "backoff")),
        lambda: _shas(repo.log_symbol(RETRY, "Retrier.run")),
        lambda: [(h.sha, h.start, h.end) for h in repo.blame(RETRY, 8, 13)],
        lambda: repo.show(shas[1]).diff,
        lambda: _shas(repo.iter_commits()),
        lambda: _shas(repo.pickaxe("MAX_BACKOFF")),
        lambda: _shas(repo.log_lines(RETRY, 5, 5)),
        lambda: repo.read_file(RETRY, rev=shas[0]),
        lambda: repo.remote_slug(),
        lambda: GitRepo(repo.root).head_sha(),
    ]
    expected = [call() for call in calls]
    n = 6 * len(calls)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: calls[i % len(calls)](), range(n)))
    assert results == [expected[i % len(calls)] for i in range(n)]


# ---------------------------------------------------------------------- review regressions

BOX_V1 = '''class Box:
    @property
    def size(self):
        return self._size

    @size.setter
    def size(self, value):
        self._size = value
'''
BOX_V2 = BOX_V1.replace(
    "        self._size = value\n",
    "        if value < 0:\n            raise ValueError('negative')\n        self._size = value\n",
)
BOX_V3 = BOX_V2.replace("return self._size", "return int(self._size)")


def test_log_symbol_near_traces_the_duplicate_definition_at_those_lines(tmp_path):
    """A property getter and setter share the name Box.size: lines 6-10 are the setter."""
    repo, _ = _make_repo(
        tmp_path / "box",
        [
            ("Add Box", {"box.py": BOX_V1}),
            ("Validate size in setter (#42)", {"box.py": BOX_V2}),
            ("Getter returns int (#43)", {"box.py": BOX_V3}),
        ],
    )
    setter = repo.log_symbol("box.py", "Box.size", near=(6, 10))
    assert [c.subject for c in setter] == ["Validate size in setter (#42)", "Add Box"]
    getter = repo.log_symbol("box.py", "Box.size", near=(2, 4))
    assert [c.subject for c in getter] == ["Getter returns int (#43)", "Add Box"]
    # without lines the first definition is traced, as before
    assert _shas(repo.log_symbol("box.py", "Box.size")) == _shas(getter)


def test_python_symbols_keep_repeated_definitions():
    from archaeologist.sources.git import _python_symbols

    symbols = _python_symbols(BOX_V3)
    assert (symbols["Box.size"].start, symbols["Box.size#2"].start) == (2, 6)


def test_merged_commits_of_a_no_ff_merge(tmp_path):
    root = _init(tmp_path / "merge")
    base = _commit(root, "base", {"a.py": "x = 1\n"})
    _git(root, "checkout", "-q", "-b", "fix")
    branch = _commit(root, "cap delay", {"a.py": "x = 2\n"})
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", "Merge pull request #12 from dev/fix", "fix")
    merge = _git(root, "rev-parse", "HEAD")
    repo = GitRepo(root)
    assert repo.merged_commits(merge) == [branch]
    assert repo.merged_commits(base) == []  # not a merge commit
    assert repo.merged_commits("f" * 40) == [] and repo.merged_commits("--all") == []


def test_file_sizes_match_ls_files(toy):
    repo, _ = toy
    sizes = repo.file_sizes()
    assert sorted(sizes) == repo.ls_files()
    assert sizes[RETRY] == len((repo.root / RETRY).read_bytes())


def test_shallow_clone_boundary_commit_is_marked(toy_repo_shas, tmp_path):
    src, shas = toy_repo_shas
    clone = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{src}", str(clone))
    repo = GitRepo(clone)
    assert repo.is_shallow and not GitRepo(src).is_shallow

    (only,) = repo.log_symbol(RETRY, "backoff")  # the docs-only commit c4: history is cut off
    assert only.sha == shas[3] and only.shallow_boundary
    ev = only.to_evidence()
    assert ev.meta["shallow_boundary"] is True and "shallow clone" in ev.text
    assert all(h.shallow_boundary for h in repo.blame(RETRY, 8, 12))
    assert repo.show(shas[3]).shallow_boundary
    full = GitRepo(src).log_symbol(RETRY, "backoff")
    assert not any(c.shallow_boundary for c in full)
    assert "shallow_boundary" not in full[0].to_evidence().meta


def test_rename_of_a_large_class_is_followed_quickly(tmp_path):
    import random
    import time

    rng = random.Random(1)
    words = ["self", "value", "result", "data", "key", "item", "count"]
    body = "\n".join(
        f"    def m{i}(self, x):\n"
        + "\n".join(
            f"        {rng.choice(words)}_{j} = {rng.choice(words)}(x, {i}, {j})" for j in range(28)
        )
        for i in range(20)
    )
    repo, shas = _make_repo(
        tmp_path / "big",
        [
            ("add Old", {"big.py": "class Old:\n" + body + "\n"}),
            ("rename Old -> New", {"big.py": "class New:\n" + body + "\n"}),
        ],
    )
    started = time.perf_counter()
    assert _shas(repo.log_symbol("big.py", "New", max_count=6)) == shas[::-1]
    assert time.perf_counter() - started < 2.0  # character matching took ~3 s here


def test_renamed_from_compares_large_sources_by_line():
    import time

    from archaeologist.sources.git import _python_symbols, _renamed_from

    lines = [f"        v{i} = compute(x, {i}, 'some text {i * 7}')" for i in range(500)]
    old = "class Old:\n    def run(self, x):\n" + "\n".join(lines) + "\n"
    new = old.replace("class Old:", "class New:")
    par_syms, cur_syms = _python_symbols(old), _python_symbols(new)
    started = time.perf_counter()
    got = _renamed_from(old, par_syms, cur_syms, new.rstrip("\n"), cur_syms["New"], "New")
    assert got == "Old"
    assert time.perf_counter() - started < 1.0  # ~25K characters: >10 s character by character


def test_stream_timeout_stops_a_slow_git(toy, monkeypatch):
    import time

    repo, _ = toy
    script = (
        "import sys, time\n"
        "sys.stdout.write('\\x1edone one\\x1edone two\\x1ecut off'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(repo, "_cmd", lambda args: [sys.executable, "-c", script])
    started = time.perf_counter()
    assert list(repo._stream(["log"], timeout=0.5)) == ["done one", "done two"]
    assert list(repo.iter_commits(timeout=0.5)) == []  # garbage records parse to nothing
    assert time.perf_counter() - started < 10
