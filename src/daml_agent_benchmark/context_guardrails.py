import os
from typing import Callable

from daml_agent_benchmark.tasklist_catalog import path_relative_to_repo
from daml_agent_benchmark.repos.registry import handler_for


def _get_common_disallowed_reasons(file_path: str, impl_files: list[str], test_file_path: str) -> list[bool]:
    abs_file_path = os.path.abspath(file_path)
    impl_file_paths_abs = {os.path.abspath(f) for f in impl_files}
    abs_test_file_path = os.path.abspath(test_file_path)

    rel_file_path = path_relative_to_repo(abs_file_path)
    rel_impl_files = [path_relative_to_repo(f) for f in impl_file_paths_abs]
    rel_test_file = path_relative_to_repo(abs_test_file_path)

    return [
        # Ensure we don't include the files we're trying to implement
        (abs_file_path in impl_file_paths_abs) or (rel_file_path in rel_impl_files),
        # No need to include test_file_path either, because it is already part of the context.
        (abs_file_path == abs_test_file_path) or (rel_file_path == rel_test_file),
    ]


def is_duplicate_or_generated_daml_context_path(file_path: str) -> bool:
    abs_file_path = os.path.abspath(file_path)
    norm = abs_file_path.replace("\\", "/")
    return any(
        [
            # Kinda hacky: some "LATEST-TRY.daml" files snuck into my index,
            # and now I'm too lazy to remake it, but those should be excluded of course.
            norm.endswith("-LATEST_TRY.daml"),
            # For daml-finance, docs/generated/src are a duplicate of src.
            ("daml-finance" in norm) and ("/docs/generated/src/" in norm),
            # For daml-finance, package/main/daml is a packaged mirror of src/main/daml.
            ("daml-finance" in norm) and ("/package/main/daml/" in norm),
            # Dependencies folders can also contain duplicates of the to-be-implemented files.
            "/.daml/dependencies/" in norm,
            # Daml package DB can include decompiled/generated duplicates.
            "/.daml/package-database/" in norm,
            # Local history snapshots are often near-identical and should not be exposed.
            "/.history/" in norm,
        ]
    )


def _is_outside_target_packages(path: str, target_package_roots: frozenset[str]) -> bool:
    """True if `path` is neither inside a target package nor on the path down to one.

    Ancestors of a target package (e.g. the repository root) are kept so the copy can
    descend into the target package; everything else (sibling packages, unrelated
    root-level files) is excluded.
    """
    ap = os.path.abspath(path)
    for pkg in target_package_roots:
        if ap == pkg or ap.startswith(pkg + os.sep):
            return False  # inside the target package
        if pkg.startswith(ap + os.sep):
            return False  # ancestor directory leading to the target package
    return True


def make_repo_copy_path_filter(
    repo_root: str,
    target_files: list[str],
) -> Callable[[str], bool]:
    """Build the exclusion filter for a task's copy: True means drop the path.

    The copy starts as the whole repository. Two kinds of paths are dropped.
    Always: duplicate and generated files (`.daml/dependencies`, docs mirrors, editor
    history) and the subtrees the repository's handler excludes outright. For repositories
    whose handler sets `repo_copy_scope_root`, everything outside the target package as well:
    each target file (an implementation file the task empties) is mapped to the directory
    that should be the copy's whole content, its package directory for ex-models and
    canton, the tutorial directory for the daml SDK tutorials. Neighbouring packages there
    contain near-copies of the answer, and the task's package builds without them.
    Scoping engages only when every target file maps to a scope root.
    """
    handler = handler_for(os.path.basename(os.path.normpath(repo_root)))
    package_roots: frozenset[str] = frozenset()
    if handler.repo_copy_scope_root is not None and target_files:
        # Scoping engages only when every target resolves to a scope root.
        roots = {handler.repo_copy_scope_root(f) for f in target_files}
        if None not in roots:
            package_roots = frozenset(os.path.abspath(r) for r in roots if r is not None)
    excluded_roots = tuple(
        os.path.abspath(os.path.join(repo_root, subpath)) for subpath in handler.excluded_subpaths
    )

    def path_filter(path: str) -> bool:
        if is_duplicate_or_generated_daml_context_path(path):
            return True
        if package_roots and _is_outside_target_packages(path, package_roots):
            return True
        abs_path = os.path.abspath(path)
        if any(abs_path == root or abs_path.startswith(root + os.sep) for root in excluded_roots):
            return True
        return False

    return path_filter
