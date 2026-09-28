"""Fetch the public repositories that the benchmark tasks come from.

Each repository is fetched at the commit that its tasks were written against. The
repositories and their commits come from the task list's `repos.yaml`, so this module and
the task list cannot disagree.

Run it as:

    uv run python -m daml_agent_benchmark.fetch_sources                   # every repository
    uv run python -m daml_agent_benchmark.fetch_sources splice ex-models  # only these

Repositories go into `locations.sources_root`, which is where the harness looks for them.
`--sources-dir PATH` puts them somewhere else. Point the harness there with
`configure(sources_root=...)` (see docs/running.md).

Running it again is safe:

- A checkout that is already at its pinned commit is left alone.
- A checkout with no commit, or at another commit, is fetched again. This repairs a fetch
  that stopped partway.
- A checkout whose tracked files have local changes is never touched. It is reported and
  skipped. Untracked files do not count, since checking out another commit keeps them.
- A folder that is not a git checkout of its own is never touched.

At the end it lists the repositories that are ready, skipped or failed. It exits with
status 1 when any repository failed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from daml_agent_benchmark.locations import locations
from daml_agent_benchmark.tasklist_catalog import Repo, add_tasklist_dir_argument, add_tasklist_dirs, load_repos

# Submodules that a repository's tasks need. account-hierarchy's packages depend on the
# lib-finance library, which the repository includes as a submodule.
SUBMODULES: dict[str, tuple[str, ...]] = {"account-hierarchy": ("lib-finance",)}

# A source checkout with edits to files that git tracks is left untouched, so that
# updating it cannot overwrite the edits. The report lists at most this many of them.
_CHANGES_SHOWN = 10


class Skipped(Exception):
    """The repository was left as it is, for a reason that the message gives."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


class GitFailed(Exception):
    """A git command failed."""


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in `path` and return the finished process.

    Git never prompts for credentials here. A wrong URL fails instead of waiting for input.
    """
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, env=env, check=False)


def _git_ok(path: Path, *args: str) -> str:
    """Run git in `path` and return its output. Raise GitFailed when it fails."""
    proc = _git(path, *args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitFailed(f"git {' '.join(args)}: {detail[-1] if detail else f'exit status {proc.returncode}'}")
    return proc.stdout


def is_own_checkout(path: Path) -> bool:
    """Whether `path` is the top folder of its own git checkout.

    Git is not run at all without a `.git` inside `path`. Otherwise git would act on the
    repository that encloses the folder.
    """
    if not (path / ".git").exists():
        return False
    proc = _git(path, "rev-parse", "--show-toplevel")
    return proc.returncode == 0 and Path(proc.stdout.strip()).resolve() == path.resolve()


def _head(path: Path) -> str | None:
    """The commit that the checkout is at, or None when it has none yet."""
    proc = _git(path, "rev-parse", "-q", "--verify", "HEAD")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _changed_tracked_files(path: Path) -> list[str]:
    """Tracked files with local changes, as `git status --short` lines.

    Untracked and ignored files are left out, so build output does not count.
    """
    return _git_ok(path, "status", "--porcelain", "--untracked-files=no").splitlines()


def _fetch_pinned(name: str, repo: Repo, path: Path) -> None:
    """Fetch the pinned commit into the checkout at `path` and check it out."""
    assert repo.url is not None and repo.commit is not None
    # Fetching only the one commit is much smaller. Some servers refuse to fetch a commit
    # by its hash, and then the whole history is fetched instead.
    if _git(path, "fetch", "-q", "--depth", "1", repo.url, repo.commit).returncode != 0:
        print(f"[{name}] the server refused to fetch the commit alone; fetching the whole history", flush=True)
        _git_ok(path, "fetch", "-q", repo.url, "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*")
    proc = _git(path, "checkout", "-q", "--detach", repo.commit)
    if proc.returncode != 0:
        if "would be overwritten" in proc.stderr:
            files = [line.strip() for line in proc.stderr.splitlines() if line.startswith(("\t", "    "))]
            raise Skipped(f"untracked files in {path} would be overwritten by the pinned commit; left as it is", files)
        raise GitFailed(f"git checkout {repo.commit[:10]}: {proc.stderr.strip()}")
    if _head(path) != repo.commit:
        raise GitFailed(f"the checkout is not at {repo.commit[:10]} after fetching it")


def fetch_one(name: str, repo: Repo, path: Path) -> None:
    """Bring one repository at `path` to its pinned commit.

    Raises Skipped when the repository is left as it is, and GitFailed when fetching failed.
    """
    assert repo.url is not None and repo.commit is not None
    short = repo.commit[:10]
    if path.exists() and not is_own_checkout(path):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
        else:
            raise Skipped(f"{path} exists but is not a git checkout; left as it is (move it away to fetch again)")

    if not path.exists():
        print(f"[{name}] fetching {repo.url} at {short} into {path}", flush=True)
        path.mkdir(parents=True)
        _git_ok(path, "init", "-q")
        _git_ok(path, "remote", "add", "origin", repo.url)
        _fetch_pinned(name, repo, path)
    else:
        head = _head(path)
        changed = _changed_tracked_files(path)
        if changed:
            where = f"at {head[:10]}" if head else "with no commit checked out"
            raise Skipped(f"{path} is {where} and has local changes; left as it is", changed)
        if head == repo.commit:
            print(f"[{name}] already at {short}", flush=True)
        elif head is None:
            print(f"[{name}] {path} has no commit checked out, so an earlier fetch did not finish; fetching again", flush=True)
            _fetch_pinned(name, repo, path)
        else:
            print(f"[{name}] at {head[:10]}, not at the pinned {short}; fetching the pinned commit", flush=True)
            _fetch_pinned(name, repo, path)

    submodules = SUBMODULES.get(name, ())
    if submodules:
        print(f"[{name}] initialising submodules: {' '.join(submodules)}", flush=True)
        _git_ok(path, "submodule", "update", "-q", "--init", *submodules)


def fetch_all(repos: dict[str, Repo], dest: Path) -> dict[str, list[str]]:
    """Fetch each repository into `dest/<name>` and return the names by outcome.

    The outcomes are "ready", "skipped" and "failed".
    """
    outcome: dict[str, list[str]] = {"ready": [], "skipped": [], "failed": []}
    for name, repo in repos.items():
        try:
            fetch_one(name, repo, dest / name)
        except Skipped as skip:
            print(f"[{name}] {skip}", flush=True)
            for line in skip.details[:_CHANGES_SHOWN]:
                print(f"[{name}]   {line}", flush=True)
            if len(skip.details) > _CHANGES_SHOWN:
                print(f"[{name}]   and {len(skip.details) - _CHANGES_SHOWN} more", flush=True)
            outcome["skipped"].append(name)
        except (GitFailed, OSError) as exc:
            print(f"[{name}] failed: {exc}", file=sys.stderr, flush=True)
            outcome["failed"].append(name)
        else:
            print(f"[{name}] ready", flush=True)
            outcome["ready"].append(name)
    return outcome


def main(argv: list[str] | None = None) -> int:
    """Fetch the repositories named on the command line, or all of them."""
    parser = argparse.ArgumentParser(
        prog="python -m daml_agent_benchmark.fetch_sources",
        description="Fetch the source repositories at their pinned commits.",
    )
    parser.add_argument("repos", nargs="*", metavar="REPO", help="repositories to fetch (default: all)")
    parser.add_argument(
        "--sources-dir",
        type=Path,
        default=None,
        help=f"where to put the repositories (default: {locations.sources_root})",
    )
    add_tasklist_dir_argument(parser)
    args = parser.parse_args(argv)
    add_tasklist_dirs(args.tasklist_dir)

    known = {name: repo for name, repo in load_repos().items() if repo.url and repo.commit}
    unknown = [name for name in args.repos if name not in known]
    if unknown:
        print(f"unknown repository: {' '.join(unknown)} (known: {' '.join(known)})", file=sys.stderr)
        return 1
    selected = {name: known[name] for name in args.repos} if args.repos else known
    dest = (args.sources_dir or locations.sources_root).expanduser().resolve()

    outcome = fetch_all(selected, dest)
    print()
    if outcome["ready"]:
        print(f"Ready in {dest}: {' '.join(outcome['ready'])}")
    if outcome["skipped"]:
        print(f"Skipped (see the messages above): {' '.join(outcome['skipped'])}")
    if outcome["failed"]:
        print(f"Failed (run again to retry): {' '.join(outcome['failed'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
