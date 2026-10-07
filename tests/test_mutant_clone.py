"""The per-mutant clone of a repository copy: the same files, links and timestamps, written independently."""

import os

from daml_agent_benchmark.task_run.mutant_grading import _clone_tree


def test_a_clone_keeps_files_links_and_timestamps_and_is_written_independently(tmp_path) -> None:
    source = tmp_path / "copy"
    (source / "daml").mkdir(parents=True)
    impl = source / "daml" / "Impl.daml"
    impl.write_text("module Impl where\n", encoding="utf-8")
    os.utime(impl, (1_700_000_000, 1_700_000_000))
    (source / "daml" / "Link.daml").symlink_to("Impl.daml")

    clone = tmp_path / "copym0"
    _clone_tree(str(source), str(clone))

    cloned = clone / "daml" / "Impl.daml"
    assert cloned.read_text(encoding="utf-8") == "module Impl where\n"
    # Daml's build cache compares timestamps, so a clone must not look newer than its source.
    assert cloned.stat().st_mtime == impl.stat().st_mtime
    link = clone / "daml" / "Link.daml"
    assert link.is_symlink() and os.readlink(link) == "Impl.daml"

    cloned.write_text("module Impl where\nx = 1\n", encoding="utf-8")
    assert impl.read_text(encoding="utf-8") == "module Impl where\n"
