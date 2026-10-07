"""What the agent is handed: the prompt, the skill, the docs, and a private home.

The skill is staged once per run and copied into every task's agent home, so each task
starts from the same files and nothing an agent writes can leak into the next task.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from daml_agent_benchmark.codex_config import task_codex_config_toml
from daml_agent_benchmark.config import ExperimentConfig
from daml_agent_benchmark.tasklist_catalog import task_sdk_version


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _zip_directory(source_dir: Path, zip_path: Path, root_name: str) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(source_dir)
            zf.write(path, arcname=str(Path(root_name) / rel))


def skill_name(config: ExperimentConfig) -> str:
    """The directory name the configured skill gets in a task's agent home.

    The last component of `skill_subdir` when the source holds several skills, otherwise of the
    configured directory or git URL, so a skill keeps the name it has where it lives. Agents refer
    to a skill by that name, so it has to be the skill's own.
    """
    assert config.skill
    source = config.skill_subdir or config.skill
    return Path(source.rstrip("/")).stem.removesuffix(".git") or "skill"


def stage_skill_for_run(config: ExperimentConfig, run_dir: Path) -> dict:
    """Copy the configured skill under the run's artifacts and snapshot it for reproducibility.

    `skill` is either a directory holding the skill or a git URL to clone. Either way the run
    keeps its own copy plus a zipped snapshot, so what the agents were given can be checked later.
    """
    if not config.skill:
        raise ValueError("stage_skill_for_run called without skill")

    skill_source = config.skill
    skill_ref = config.skill_ref
    skill_subdir = config.skill_subdir
    name = skill_name(config)

    staged_skills_root = run_dir / "artifacts" / "skill_staging"
    staged_skills_root.mkdir(parents=True, exist_ok=True)
    staged_skill_dir = staged_skills_root / name
    if staged_skill_dir.exists():
        shutil.rmtree(staged_skill_dir)

    local_dir = Path(skill_source).expanduser()
    if local_dir.is_dir():
        _stage_skill_tree(local_dir, skill_subdir, staged_skill_dir, source_label=str(local_dir))
        return _skill_staging_record(
            run_dir, name, staged_skill_dir, source=str(local_dir), source_kind="directory", git_commit=None,
            git_ref=None,
        )

    if not shutil.which("git"):
        raise RuntimeError(
            f"skill is not a directory on this machine, so it is treated as a git URL, "
            f"but `git` was not found on PATH: {skill_source}"
        )

    with tempfile.TemporaryDirectory(prefix="codex-skill-") as temp_dir:
        repo_dir = Path(temp_dir) / "repo"
        clone_cmd = ["git", "clone", "--depth", "1"]
        if skill_ref:
            clone_cmd.extend(["--branch", skill_ref])
        clone_cmd.extend([skill_source, str(repo_dir)])
        clone_proc = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)
        if clone_proc.returncode != 0:
            stderr_tail = (clone_proc.stderr or "").strip() or (clone_proc.stdout or "").strip()
            raise RuntimeError(
                "Failed to clone skill. "
                f"cmd={' '.join(clone_cmd)!r}, exit={clone_proc.returncode}, output={stderr_tail}"
            )
        commit_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_proc.returncode != 0:
            stderr_tail = (commit_proc.stderr or "").strip() or (commit_proc.stdout or "").strip()
            raise RuntimeError(
                f"Failed to resolve cloned skill commit. exit={commit_proc.returncode}, output={stderr_tail}"
            )
        skill_git_commit = commit_proc.stdout.splitlines()[0]
        if not skill_git_commit:
            raise RuntimeError("Failed to resolve cloned skill commit: empty rev-parse output")

        _stage_skill_tree(repo_dir, skill_subdir, staged_skill_dir, source_label=f"{skill_source} (cloned)")

    return _skill_staging_record(
        run_dir, name, staged_skill_dir, source=skill_source, source_kind="git",
        git_commit=skill_git_commit, git_ref=skill_ref,
    )


def _stage_skill_tree(root: Path, subdir: str, staged_skill_dir: Path, *, source_label: str) -> None:
    """Copy a skill directory into the run's staging area, without any `.git`."""
    skill_source_dir = root / subdir if subdir else root
    if not skill_source_dir.is_dir():
        raise RuntimeError(f"Configured skill_subdir does not exist in {source_label}: {skill_source_dir}")
    if not (skill_source_dir / "SKILL.md").exists():
        raise RuntimeError(
            f"Configured skill source is not a valid skill directory (missing SKILL.md): {skill_source_dir}"
        )
    shutil.copytree(skill_source_dir, staged_skill_dir, ignore=shutil.ignore_patterns(".git"))


def _skill_staging_record(
    run_dir: Path,
    name: str,
    staged_skill_dir: Path,
    *,
    source: str,
    source_kind: str,
    git_commit: str | None,
    git_ref: str | None,
) -> dict:
    """Zip the staged skill and describe what was staged.

    The zip's sha256 identifies the content whichever kind of source it came from; a cloned skill
    also records its commit.
    """
    label = git_commit[:12] if git_commit else source_kind
    skill_zip_path = run_dir / "artifacts" / "skills" / f"{name}.{label}.zip"
    _zip_directory(staged_skill_dir, skill_zip_path, root_name=name)
    skill_zip_sha256 = _sha256_file(skill_zip_path)
    print(f"[skills] staged skill from {source} at {staged_skill_dir}", flush=True)
    print(
        f"[skills] skill snapshot {skill_zip_path} "
        f"(commit={git_commit or 'n/a'}, sha256={skill_zip_sha256})",
        flush=True,
    )
    return {
        "staged_dir": str(staged_skill_dir),
        "snapshot_zip": str(skill_zip_path),
        "snapshot_zip_sha256": skill_zip_sha256,
        "source": source,
        "source_kind": source_kind,
        "git_commit": git_commit,
        "git_ref": git_ref,
    }


# The documentation each Daml generation wants, by the prefixes of the SDK versions it covers.
# The first match wins; a version none of them claims gets `_LATEST_DOCS`.
_DOCS_BY_SDK_PREFIX: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("1.", "2."), ("daml2_docs.json", "daml2_docs_toc.json")),
    (("3.0", "3.1", "3.2", "3.3"), ("daml3_3_docs.json", "daml3_3_docs_toc.json")),
    (("3.4",), ("daml3_4_docs.json", "daml3_4_docs_toc.json", "canton_network_docs.json")),
)
_LATEST_DOCS = ("daml3_5_docs.json", "daml3_5_docs_toc.json", "canton_network_docs.json")


def task_docs_for_sdk(sdk_version: str) -> tuple[str, ...]:
    """The documentation files for one SDK version."""
    version = sdk_version.strip()
    for prefixes, docs in _DOCS_BY_SDK_PREFIX:
        if version.startswith(prefixes):
            return docs
    return _LATEST_DOCS


def stage_task_docs(config: ExperimentConfig, test_file_path: str, repo_copy_root: str) -> list[str]:
    """Put the documentation for this task's Daml generation in `docs/` inside the copy.

    The container has no egress, so this is the only documentation the agent can reach. A
    file that is asked for and missing raises: a task that ran without it is not comparable
    with one that had it, and a warning in the terminal is not something the records keep.
    """
    docs_src_dir = Path(config.task_docs_dir)
    docs_dest_dir = Path(repo_copy_root) / "docs"
    docs_dest_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for doc_file in task_docs_for_sdk(task_sdk_version(test_file_path)):
        source = docs_src_dir / doc_file
        if not source.exists():
            raise FileNotFoundError(f"task_docs_dir has no {doc_file}. expected={source}")
        shutil.copy2(source, docs_dest_dir / doc_file)
        staged.append(doc_file)
    return staged


def stage_skill_into_copy(config: ExperimentConfig, run_dir: Path | None, repo_copy_root: str) -> None:
    """Put the run's staged skill inside this task's copy, replacing anything already there.

    The run stages the skill once; every task gets its own copy of those files, so nothing
    an agent writes to them can reach the next task. The staging lives under the run
    directory, so a task run without one cannot have a skill.
    """
    if run_dir is None:
        raise ValueError("run_dir is required when skill is set")
    source = run_dir / "artifacts" / "skill_staging" / skill_name(config)
    if not source.is_dir():
        raise RuntimeError(f"Skill staged directory missing for repository-copy preparation. expected={source}")
    destination = Path(repo_copy_root) / TASK_SKILL_STAGING_REL
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)


def blank_files(paths: list[str]) -> None:
    """Empty each file: the answer files, so the agent writes them from scratch."""
    for path in paths:
        with open(path, "w", encoding="utf-8") as f:
            f.write("")


def build_codex_prompt(
    test_file_path: str,
    impl_files: list[str],
    repo_copy_root: str,
    docs_skill_content: str | None = None,
) -> str:
    """Construct the prompt that tells Codex what task to solve. Uses relative paths
    so the prompt works inside the copy regardless of where it's mounted."""
    rel_test = os.path.relpath(test_file_path, repo_copy_root)
    rel_impls = [os.path.relpath(p, repo_copy_root) for p in impl_files]
    impl_list = "\n".join(f"- {p}" for p in rel_impls)
    prompt = f"""
You are solving one DAML benchmark task.

Goal:
- Make the test file pass by implementing/fixing only the listed implementation files.
- You may run shell commands as needed (lint/build/test).

Constraints:
- Test file: {rel_test}
- Implementation files you may edit:
{impl_list}
- Do not edit unrelated files.
- Stop when tests pass or when you are blocked.

When done, reply with a short summary of how you solved it and whether tests passed.
"""
    if docs_skill_content:
        prompt += f"\n\nAdditional documentation-navigation guidance:\n\n{docs_skill_content.rstrip()}\n"
    return prompt


def strip_leading_yaml_front_matter(markdown: str) -> str:
    lines = markdown.splitlines()
    if len(lines) >= 3 and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                stripped = "\n".join(lines[idx + 1 :]).lstrip("\n")
                return stripped
    return markdown


def load_prompt_guidance(config: ExperimentConfig) -> str:
    """The configured guidance markdown, without a leading YAML front matter block."""
    assert config.prompt_guidance_file
    guidance_md = Path(config.prompt_guidance_file)
    if not guidance_md.is_file():
        raise RuntimeError(f"Prompt guidance file not found: {guidance_md}")
    return strip_leading_yaml_front_matter(guidance_md.read_text(encoding="utf-8"))


_TASK_CODEX_HOME_REL = ".codex_home"
TASK_SKILL_STAGING_REL = ".skill_staged"


def sanitize_copyback_rel_paths(copyback_rel_paths: list[str]) -> list[str]:
    """Return a normalized, safe, deduplicated list of relative copyback paths.

    These paths are consumed by the container wrapper during cleanup to copy back
    only implementation files instead of the whole workspace. We normalize path
    separators, validate non-empty safe relative paths, and preserve input order.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for idx, raw in enumerate(copyback_rel_paths, start=1):
        rel = str(raw).replace("\\", "/").strip()
        while rel.startswith("./"):
            rel = rel[2:]
        rel = rel.lstrip("/")
        if not rel:
            raise ValueError(f"copyback_rel_paths[{idx}] became empty after normalization (raw={raw!r})")
        if rel == ".." or rel.startswith("../") or "/../" in rel or rel.endswith("/.."):
            raise ValueError(f"copyback_rel_paths[{idx}] is unsafe (path traversal): {raw!r}")
        if rel in seen:
            continue
        seen.add(rel)
        cleaned.append(rel)
    return cleaned


def configure_copyback_rel_paths_env(codex_env: dict[str, str], copyback_rel_paths: list[str]) -> None:
    """Configure per-task impl-only copyback paths for the container wrapper.

    The wrapper checks `CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS`:
    - set: copy back only the listed relative paths
    - unset: copy back the full workspace

    We always clear any inherited value first to avoid stale path leakage across
    tasks, then set a sanitized newline-delimited list.

    Fail-fast behavior:
    - if `copyback_rel_paths` is empty, raise
    - if any provided path is unsafe/invalid after normalization, raise

    This avoids silently falling back to full-workspace copyback when impl-only
    copyback was expected.
    """
    codex_env.pop("CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS", None)
    if len(copyback_rel_paths) == 0:
        raise ValueError("copyback_rel_paths is empty; refusing implicit full-workspace copyback.")
    cleaned = sanitize_copyback_rel_paths(copyback_rel_paths)
    codex_env["CONTAINER_AGENT_EVAL_COPYBACK_REL_PATHS"] = "\n".join(cleaned)


def prepare_task_codex_home(config: ExperimentConfig, repo_copy_root: str) -> str:
    """Create the task's private CODEX_HOME under the copy.

    It holds a generated `config.toml` and, when enabled, the staged skill:
    nothing from any other codex home. The API key never touches it (codex reads
    CODEX_API_KEY from the container environment), and no session or memory state
    can carry over from one task to the next.
    """
    task_codex_home = Path(repo_copy_root) / _TASK_CODEX_HOME_REL
    if task_codex_home.exists():
        shutil.rmtree(task_codex_home)
    task_codex_home.mkdir(parents=True, exist_ok=True)
    (task_codex_home / "config.toml").write_text(task_codex_config_toml(config), encoding="utf-8")

    if config.skill:
        source_skill_dir = Path(repo_copy_root) / TASK_SKILL_STAGING_REL
        if not source_skill_dir.is_dir():
            raise RuntimeError(
                f"Skill source directory missing for task-local CODEX_HOME staging. expected={source_skill_dir}"
            )

        dest_skill_dir = task_codex_home / "skills" / skill_name(config)
        dest_skill_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_skill_dir, dest_skill_dir)

    return _TASK_CODEX_HOME_REL
