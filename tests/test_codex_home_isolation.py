"""Tests for the task-local codex home.

`prepare_task_codex_home` creates `<repository copy>/.codex_home` for every task. It
holds a generated `config.toml` and, when configured, the staged skill, and
nothing else: no API key file (codex reads `CODEX_API_KEY` from the container
environment) and no session or memory state that could carry over between tasks.
"""

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from daml_agent_benchmark.codex_config import CODEX_TASK_CONFIG
from daml_agent_benchmark.config import DEFAULTS
from daml_agent_benchmark.task_run.inputs import (
    TASK_SKILL_STAGING_REL,
    build_codex_prompt,
    load_prompt_guidance,
    prepare_task_codex_home,
    skill_name,
    stage_skill_for_run,
    strip_leading_yaml_front_matter,
)


def test_task_codex_home_holds_only_the_generated_config(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    repo_copy_root.mkdir()
    config = DEFAULTS.merge(SimpleNamespace(skill=None))

    codex_home_rel = prepare_task_codex_home(config, str(repo_copy_root))

    task_home = repo_copy_root / ".codex_home"
    assert codex_home_rel == ".codex_home"
    assert [p.name for p in task_home.iterdir()] == ["config.toml"]

    config_toml = tomllib.loads((task_home / "config.toml").read_text(encoding="utf-8"))
    assert config_toml == CODEX_TASK_CONFIG


def test_generated_config_carries_transport_and_hardening() -> None:
    provider = CODEX_TASK_CONFIG["model_providers"]["openai-https"]
    assert CODEX_TASK_CONFIG["model_provider"] == "openai-https"
    assert provider["prefer_websockets"] is False
    assert provider["env_key"] == "OPENAI_API_KEY"
    assert CODEX_TASK_CONFIG["web_search"] == "disabled"
    assert CODEX_TASK_CONFIG["features"]["browser_use"] is False
    assert CODEX_TASK_CONFIG["features"]["multi_agent"] is True
    assert CODEX_TASK_CONFIG["features"]["multi_agent_v2"] is False
    assert CODEX_TASK_CONFIG["shell_environment_policy"]["ignore_default_excludes"] is False
    assert "OPENAI_API_KEY" in CODEX_TASK_CONFIG["shell_environment_policy"]["exclude"]
    # Skills and image generation stay available to the agent.
    assert "skills" not in CODEX_TASK_CONFIG
    assert "image_generation" not in CODEX_TASK_CONFIG["features"]


def test_task_codex_home_is_recreated_from_scratch(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    stale_home = repo_copy_root / ".codex_home"
    (stale_home / "sessions").mkdir(parents=True)
    (stale_home / "auth.json").write_text('{"OPENAI_API_KEY":"sk-secret"}\n', encoding="utf-8")
    (stale_home / "memories_1.sqlite").write_bytes(b"sqlite")
    config = DEFAULTS.merge(SimpleNamespace(skill=None))

    prepare_task_codex_home(config, str(repo_copy_root))

    assert [p.name for p in stale_home.iterdir()] == ["config.toml"]


def test_prepare_task_codex_home_with_skill_uses_staged_skill_dir(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    repo_copy_root.mkdir()
    staged_skill_dir = repo_copy_root / TASK_SKILL_STAGING_REL
    staged_skill_dir.mkdir(parents=True)
    (staged_skill_dir / "SKILL.md").write_text("staged skill content\n", encoding="utf-8")
    config = DEFAULTS.merge(SimpleNamespace(skill="/skills/example-skill"))

    prepare_task_codex_home(config, str(repo_copy_root))

    copied_skill = repo_copy_root / ".codex_home" / "skills" / skill_name(config) / "SKILL.md"
    assert copied_skill.is_file()
    assert copied_skill.read_text(encoding="utf-8") == "staged skill content\n"


def test_prepare_task_codex_home_with_skill_requires_staged_skill_dir(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    repo_copy_root.mkdir()
    config = DEFAULTS.merge(SimpleNamespace(skill="/skills/example-skill"))

    with pytest.raises(RuntimeError, match="Skill source directory missing"):
        prepare_task_codex_home(config, str(repo_copy_root))


def test_prepare_task_codex_home_with_docs_enabled_does_not_copy_docs_skill(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    repo_copy_root.mkdir()
    config = DEFAULTS.merge(SimpleNamespace(skill=None, prompt_guidance_file=str(tmp_path / "guidance.md")))

    prepare_task_codex_home(config, str(repo_copy_root))

    assert not (repo_copy_root / ".codex_home" / "skills").exists()


def test_build_codex_prompt_appends_docs_skill_content(tmp_path) -> None:
    repo_copy_root = tmp_path / "repo_copy"
    test_file = repo_copy_root / "src" / "test.daml"
    impl_file = repo_copy_root / "src" / "impl.daml"
    docs_skill = "---\nname: guidance\n---\n# Guidance\n"

    prompt = build_codex_prompt(
        str(test_file),
        [str(impl_file)],
        str(repo_copy_root),
        docs_skill_content=docs_skill,
    )

    assert "Additional documentation-navigation guidance" in prompt
    assert "# Guidance" in prompt
    assert "Test file: src/test.daml" in prompt
    assert "- src/impl.daml" in prompt


def test_strip_leading_yaml_front_matter() -> None:
    markdown = "---\nname: guidance\ndescription: x\n---\n\n# Heading\nBody\n"
    stripped = strip_leading_yaml_front_matter(markdown)
    assert stripped.startswith("# Heading")
    assert "name: guidance" not in stripped


def test_load_prompt_guidance_strips_front_matter(tmp_path) -> None:
    guidance = tmp_path / "SKILL.md"
    guidance.write_text("---\nname: guidance\n---\n# Guidance\n", encoding="utf-8")

    content = load_prompt_guidance(DEFAULTS.merge(SimpleNamespace(prompt_guidance_file=str(guidance))))

    assert content.startswith("# Guidance")
    assert "name: guidance" not in content


def test_stage_skill_for_run_from_local_directory(tmp_path) -> None:
    skill_dir = tmp_path / "my-skill"
    (skill_dir / "nested").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# My skill\n", encoding="utf-8")
    (skill_dir / "nested" / "reference.md").write_text("detail\n", encoding="utf-8")
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    config = DEFAULTS.merge(SimpleNamespace(skill=str(skill_dir)))

    info = stage_skill_for_run(config, run_dir)

    staged = Path(info["staged_dir"])
    assert staged == run_dir / "artifacts" / "skill_staging" / "my-skill"
    assert (staged / "SKILL.md").read_text(encoding="utf-8") == "# My skill\n"
    assert (staged / "nested" / "reference.md").is_file()
    assert not (staged / ".git").exists()
    assert info["source_kind"] == "directory"
    assert info["git_commit"] is None
    assert Path(info["snapshot_zip"]).is_file()
    assert info["snapshot_zip_sha256"]


def test_stage_skill_for_run_local_directory_subdir(tmp_path) -> None:
    root = tmp_path / "skills-collection"
    (root / "daml").mkdir(parents=True)
    (root / "daml" / "SKILL.md").write_text("# Daml\n", encoding="utf-8")
    config = DEFAULTS.merge(SimpleNamespace(skill=str(root), skill_subdir="daml"))

    info = stage_skill_for_run(config, tmp_path / "run")

    staged = Path(info["staged_dir"])
    assert (staged / "SKILL.md").read_text(encoding="utf-8") == "# Daml\n"
    # The agent refers to the skill by name, so it is the skill's own, not the collection's.
    assert staged.name == "daml"
    assert skill_name(config) == "daml"


def test_stage_skill_for_run_local_directory_without_skill_md(tmp_path) -> None:
    skill_dir = tmp_path / "not-a-skill"
    skill_dir.mkdir()
    config = DEFAULTS.merge(SimpleNamespace(skill=str(skill_dir)))

    with pytest.raises(RuntimeError, match="missing SKILL.md"):
        stage_skill_for_run(config, tmp_path / "run")
