"""The source fetcher brings checkouts to their pinned commits and never discards local work.

Local git repositories in tmp_path stand in for the remotes, so no test needs the network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from daml_agent_benchmark import fetch_sources
from daml_agent_benchmark.tasklist_catalog import BuildTool, Repo


def _git(path: Path, *args: str) -> str:
    cmd = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()


def _commit(remote: Path, filename: str, text: str) -> str:
    (remote / filename).write_text(text, encoding="utf-8")
    _git(remote, "add", filename)
    _git(remote, "commit", "-q", "-m", f"write {filename}")
    return _git(remote, "rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, str, str]:
    """A remote with two commits. The second one is pinned."""
    path = tmp_path / "remote"
    path.mkdir()
    _git(path, "init", "-q")
    old = _commit(path, "Main.daml", "module Main where\n")
    pinned = _commit(path, "Other.daml", "module Other where\n")
    return path, old, pinned


@pytest.fixture
def run(tmp_path: Path, remote, monkeypatch):
    """Run the fetcher's command line over one repository, `demo`, backed by the remote."""
    path, _, pinned = remote
    repos = {"demo": Repo(name="demo", url=str(path), commit=pinned, license=None, build_tool=BuildTool.DAML)}
    monkeypatch.setattr(fetch_sources, "load_repos", lambda: repos)
    sources = tmp_path / "sources"

    def run_main(*args: str) -> int:
        return fetch_sources.main(["--sources-dir", str(sources), *args])

    return run_main, sources / "demo", repos


def test_fresh_fetch_then_rerun_is_a_no_op(run, remote, capsys) -> None:
    run_main, checkout, _ = run
    assert run_main() == 0
    assert _git(checkout, "rev-parse", "HEAD") == remote[2]
    capsys.readouterr()

    assert run_main("demo") == 0
    out = capsys.readouterr().out
    assert "already at" in out
    assert "Ready in" in out


def test_repairs_a_checkout_with_no_commit(run, remote, capsys) -> None:
    run_main, checkout, _ = run
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q")
    assert run_main() == 0
    assert _git(checkout, "rev-parse", "HEAD") == remote[2]
    assert "did not finish" in capsys.readouterr().out


def test_repairs_a_checkout_at_another_commit(run, remote) -> None:
    run_main, checkout, _ = run
    path, old, pinned = remote
    subprocess.run(["git", "clone", "-q", str(path), str(checkout)], check=True)
    _git(checkout, "checkout", "-q", old)
    assert run_main() == 0
    assert _git(checkout, "rev-parse", "HEAD") == pinned


def test_skips_a_checkout_with_a_modified_tracked_file(run, remote, capsys) -> None:
    run_main, checkout, _ = run
    path, old, _ = remote
    subprocess.run(["git", "clone", "-q", str(path), str(checkout)], check=True)
    _git(checkout, "checkout", "-q", old)
    (checkout / "Main.daml").write_text("module Main where\n-- my work\n", encoding="utf-8")

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "has local changes" in out
    assert "M Main.daml" in out
    assert "Skipped" in out
    assert _git(checkout, "rev-parse", "HEAD") == old
    assert "my work" in (checkout / "Main.daml").read_text(encoding="utf-8")


def test_an_untracked_file_does_not_block_a_repair(run, remote) -> None:
    run_main, checkout, _ = run
    path, old, pinned = remote
    subprocess.run(["git", "clone", "-q", str(path), str(checkout)], check=True)
    _git(checkout, "checkout", "-q", old)
    (checkout / "notes.txt").write_text("keep me\n", encoding="utf-8")

    assert run_main() == 0
    assert _git(checkout, "rev-parse", "HEAD") == pinned
    assert (checkout / "notes.txt").read_text(encoding="utf-8") == "keep me\n"


def test_an_untracked_file_in_the_way_skips_the_repair(run, remote, capsys) -> None:
    run_main, checkout, _ = run
    path, old, _ = remote
    subprocess.run(["git", "clone", "-q", str(path), str(checkout)], check=True)
    _git(checkout, "checkout", "-q", old)
    (checkout / "Other.daml").write_text("my own file\n", encoding="utf-8")

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "would be overwritten" in out
    assert "Other.daml" in out
    assert (checkout / "Other.daml").read_text(encoding="utf-8") == "my own file\n"


def test_a_folder_that_is_not_a_checkout_is_skipped(run, capsys) -> None:
    run_main, checkout, _ = run
    checkout.mkdir(parents=True)
    (checkout / "file.txt").write_text("not a repository\n", encoding="utf-8")

    assert run_main() == 0
    assert "not a git checkout" in capsys.readouterr().out
    assert not (checkout / ".git").exists()


def test_an_unknown_name_exits_1_before_fetching(run, capsys) -> None:
    run_main, checkout, _ = run
    assert run_main("demo", "nope") == 1
    err = capsys.readouterr().err
    assert "unknown repository: nope" in err
    assert "known: demo" in err
    assert not checkout.exists()


def test_a_failed_fetch_exits_1(run, tmp_path, capsys) -> None:
    run_main, _, repos = run
    repos["demo"] = Repo(name="demo", url=str(tmp_path / "missing"), commit="0" * 40, license=None, build_tool=BuildTool.DAML)
    assert run_main() == 1
    err = capsys.readouterr().err
    assert "[demo] failed" in err
    assert "Failed (run again to retry): demo" in err
