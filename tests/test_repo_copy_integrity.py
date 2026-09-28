"""Per-run repository-copy integrity scan: ground-truth containment, DAR entries, .git, symlinks."""

import os
import zipfile
from pathlib import Path

import pytest

from daml_agent_benchmark.repo_copy_integrity import (
    RepoCopyIntegrityError,
    assert_repo_copy_integrity,
    describe_integrity_failure,
    prune_dars_containing_targets,
    scan_repo_copy_integrity,
)

TARGET_BODY = "module Impl where\n\ntemplate Foo\n  with\n    owner : Party\n  where\n    signatory owner\n"


# Long enough to exercise the reformatted and partial checks, which ignore anything
# too short to be more than boilerplate.
LONG_TARGET_BODY = """module Impl where

import DA.Optional (fromOptional)

template Vault
  with
    operator : Party
    owner : Party
    balance : Decimal
    label : Text
  where
    signatory operator
    observer owner
    ensure balance >= 0.0

    choice Deposit : ContractId Vault
      with amount : Decimal
      controller owner
      do
        assertMsg "deposit must be positive" (amount > 0.0)
        create this with balance = balance + amount

    choice Withdraw : ContractId Vault
      with amount : Decimal
      controller owner
      do
        assertMsg "insufficient balance" (amount <= balance)
        create this with balance = balance - amount
"""


def _make_repo_copy(tmp_path: Path) -> tuple[Path, Path]:
    repo_copy = tmp_path / "repo_____abcd1234"
    (repo_copy / "daml").mkdir(parents=True)
    target = repo_copy / "daml" / "Impl.daml"
    target.write_text(TARGET_BODY, encoding="utf-8")
    (repo_copy / "daml" / "Test.daml").write_text("module Test where\nimport Impl\n", encoding="utf-8")
    (repo_copy / "daml.yaml").write_text("sdk-version: 2.9.0\n", encoding="utf-8")
    return repo_copy, target


def test_clean_copy_passes_and_has_digest(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is True
    assert report["offending"] == []
    assert report["files_scanned"] == 2  # target itself is skipped
    assert len(report["repo_copy_digest"]) == 64
    assert_repo_copy_integrity(report)


def test_digest_changes_with_content(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    before = scan_repo_copy_integrity(repo_copy, [str(target)])["repo_copy_digest"]
    (repo_copy / "daml.yaml").write_text("sdk-version: 2.10.0\n", encoding="utf-8")
    after = scan_repo_copy_integrity(repo_copy, [str(target)])["repo_copy_digest"]
    assert before != after


def test_detects_plain_copy_and_containment(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    (repo_copy / "daml" / "ImplCopy.daml").write_text(TARGET_BODY, encoding="utf-8")
    (repo_copy / "notes.md").write_text("reference:\n" + TARGET_BODY + "\nend", encoding="utf-8")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is False
    offending_paths = sorted(o["path"] for o in report["offending"])
    assert offending_paths == ["daml/ImplCopy.daml", "notes.md"]
    with pytest.raises(RepoCopyIntegrityError, match="ground-truth content"):
        assert_repo_copy_integrity(report)


def test_detects_target_inside_dar(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    dar_path = repo_copy / ".lib" / "impl-1.0.0.dar"
    dar_path.parent.mkdir()
    # Real DARs are deflated, so the target text is not visible in the raw archive bytes.
    with zipfile.ZipFile(dar_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("impl-1.0.0/daml/Impl.daml", TARGET_BODY)
        archive.writestr("impl-1.0.0/Impl.dalf", b"\x00binary")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    by_entry = {(o["archive_entry"], o["kind"]) for o in report["offending"]}
    assert by_entry == {
        ("impl-1.0.0/daml/Impl.daml", "archive_entry_content"),
        ("impl-1.0.0/daml/Impl.daml", "archive_entry_name"),
        # The compiled form of a target module is the answer too, so its name counts.
        ("impl-1.0.0/Impl.dalf", "archive_entry_name"),
    }
    assert all(o["path"] == ".lib/impl-1.0.0.dar" for o in report["offending"])


def test_dependency_dar_with_same_module_basename_is_not_offending(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    target.write_text(
        "module Example.Event.Service where\n\ntemplate Service\n  with\n    operator : Party\n  where\n    signatory operator\n",
        encoding="utf-8",
    )
    dar_path = repo_copy / "lib" / "some-dep-1.0.0.dar"
    dar_path.parent.mkdir()
    with zipfile.ZipFile(dar_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("some-dep-1.0.0-abc/Dep/Clearing/Service.daml", "module Dep.Clearing.Service where\n")
        archive.writestr("some-dep-1.0.0-abc/Dep/Clearing/Service.dalf", b"\x00")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is True, report["offending"]

    with zipfile.ZipFile(dar_path, "a", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("leak-1.0.0-def/Example/Event/Service.daml", "module Example.Event.Service where\n")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert [o["kind"] for o in report["offending"]] == ["archive_entry_name"]


def test_symlink_alias_of_target_is_not_offending(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    (repo_copy / "test-pkg").mkdir()
    os.symlink(target, repo_copy / "test-pkg" / "Impl.daml")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is True


def test_detects_git_dir_and_escaping_symlink(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    (repo_copy / ".git").mkdir()
    (repo_copy / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    os.symlink(outside, repo_copy / "link-out")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is False
    assert report["git_entries"] == [".git"]
    assert report["symlinks_outside_repo_copy"] == [{"path": "link-out", "resolves_to": str(outside.resolve())}]
    # .git contents are not scanned or hashed
    assert report["files_scanned"] == 2


def test_detects_escaping_directory_symlink(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    outside_dir = tmp_path / "other-repository"
    outside_dir.mkdir()
    (outside_dir / "Secret.daml").write_text("module Secret where\n", encoding="utf-8")
    os.symlink(outside_dir, repo_copy / "deps", target_is_directory=True)
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is False
    assert report["symlinks_outside_repo_copy"] == [{"path": "deps", "resolves_to": str(outside_dir.resolve())}]
    assert report["files_scanned"] == 2


def test_digest_changes_when_only_a_target_changes(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    before = scan_repo_copy_integrity(repo_copy, [str(target)])["repo_copy_digest"]
    target.write_text(TARGET_BODY + "\n-- changed\n", encoding="utf-8")
    after = scan_repo_copy_integrity(repo_copy, [str(target)])["repo_copy_digest"]
    assert before != after


def test_short_targets_fail_the_scan_because_a_copy_would_go_undetected(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    target.write_text("module X where\n", encoding="utf-8")
    (repo_copy / "other.daml").write_text("module X where\n", encoding="utf-8")
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    # Too short to fingerprint means the copy next to it was never checked, which is
    # an unverifiable task rather than a clean one.
    assert report["ok"] is False
    assert report["targets_skipped_too_short"] == [str(target)]
    assert "too short to fingerprint" in describe_integrity_failure(report)


def test_prune_removes_only_dars_carrying_a_target(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    target.write_text("module Impl where\n\n" + TARGET_BODY, encoding="utf-8")

    # A dependency archive the build needs, with no target module in it.
    keep = repo_copy / ".lib" / "daml-ctl-2.5.0.dar"
    keep.parent.mkdir(parents=True)
    with zipfile.ZipFile(keep, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("daml-ctl-2.5.0-abc/DA/Ctl/Version.daml", "module DA.Ctl.Version where\n")

    # A prebuilt archive of the package under test: the answer in compiled form.
    leak = repo_copy / ".lib" / "impl-1.0.0.dar"
    with zipfile.ZipFile(leak, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("impl-1.0.0-def/Impl.daml", target.read_text(encoding="utf-8"))

    assert scan_repo_copy_integrity(repo_copy, [str(target)])["ok"] is False
    removed = prune_dars_containing_targets(repo_copy, [str(target)])

    assert [r["path"] for r in removed] == [".lib/impl-1.0.0.dar"]
    assert removed[0]["entries"] == ["impl-1.0.0-def/Impl.daml"]
    assert not leak.exists(), "the leaking archive is deleted"
    assert keep.exists(), "a dependency archive without a target module is kept"
    assert scan_repo_copy_integrity(repo_copy, [str(target)])["ok"] is True


def test_prune_is_a_no_op_when_nothing_leaks(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    assert prune_dars_containing_targets(repo_copy, [str(target)]) == []


def test_detects_a_copy_whose_layout_and_comments_differ(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    target.write_text(LONG_TARGET_BODY, encoding="utf-8")
    body = target.read_text(encoding="utf-8")
    # The same code with CRLF endings, doubled indentation and a comment added: an
    # exact search finds nothing.
    disguised = "\r\n".join("  " + line if line.startswith(" ") else line for line in body.splitlines())
    disguised = disguised.replace("template Vault", "-- reference implementation\r\ntemplate Vault")
    (repo_copy / "notes").mkdir()
    (repo_copy / "notes" / "Reference.daml").write_text(disguised, encoding="utf-8")

    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is False
    assert [(o["path"], o["kind"]) for o in report["offending"]] == [("notes/Reference.daml", "content_reformatted")]


def test_detects_a_copy_of_part_of_a_target(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    target.write_text(LONG_TARGET_BODY, encoding="utf-8")
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 10, "fixture must be long enough for a partial copy to be meaningful"
    # Only the second half, so the whole-target checks cannot fire.
    (repo_copy / "Half.daml").write_text("\n".join(lines[len(lines) // 2 :]) + "\n", encoding="utf-8")

    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    # Reported, not fatal: sibling modules in these libraries duplicate real code.
    assert report["ok"] is True
    assert report["offending"] == []
    assert [(o["path"], o["kind"]) for o in report["partial_matches"]] == [("Half.daml", "content_partial")]


def test_shared_boilerplate_is_not_reported(tmp_path: Path) -> None:
    repo_copy, target = _make_repo_copy(tmp_path)
    # Lines every Daml module has, and short ones the window filter drops.
    (repo_copy / "Other.daml").write_text(
        "module Other where\n\ntemplate Bar\n  with\n    owner : Party\n  where\n    signatory owner\n",
        encoding="utf-8",
    )
    report = scan_repo_copy_integrity(repo_copy, [str(target)])
    assert report["ok"] is True, report["offending"]
