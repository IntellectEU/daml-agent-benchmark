"""ex-models: independent example packages in one repository."""

from daml_agent_benchmark.repos.sdk_version import find_daml_yaml_root
from daml_agent_benchmark.repos.registry import RepoHandler, register

# Each example is a self-contained package, and the other examples often contain the
# answer in another form (the `voting` package reproduces the `governance` Ballot template
# verbatim), so a task's copy holds its own package only.
register(RepoHandler(name="ex-models", repo_copy_scope_root=lambda target_file: find_daml_yaml_root(target_file)))
