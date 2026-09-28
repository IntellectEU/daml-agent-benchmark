"""Capture the environment a repo's .envrc produces, using direnv itself.

Some repositories parameterize daml.yaml (sdk-version, package versions, DAR paths) with
${VAR} placeholders that direnv exports from .envrc. Container flows can't run
direnv where the variables are needed (direnv activates via interactive-shell
hooks or explicit `direnv exec` wrapping — bare `docker exec` has neither, and
the agent chooses its own commands), so the harness captures the environment
once on the host and injects it into containers as plain environment variables.

The capture runs the real `direnv export json` rather than any hand-rolled
.envrc parsing, so it matches a developer machine exactly — including direnv
stdlib features, should a repo use them.

Security note: capture must only ever run on TRUSTED content — the pristine
copy at prep time, before the agent runs — because it executes the .envrc.
The captured dict is then reused for the post-agent eval container; nothing is
re-evaluated after the agent had write access.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

# Host/session-specific variables that .envrc evaluation may touch but that must
# never leak into containers (they would clobber the container's own paths).
_EXCLUDED_VAR_NAMES = {"PATH", "HOME", "PWD", "OLDPWD", "SHELL", "SHLVL", "TMPDIR", "XDG_DATA_HOME", "XDG_CONFIG_HOME"}


def capture_envrc_environment(repo_root: str | Path) -> dict[str, str]:
    """Return the env-var changes that direnv-loading `<repo_root>/.envrc` produces.

    Empty dict if there is no .envrc. Raises RuntimeError if direnv is missing or
    fails — a silent empty result would surface later as cryptic daml.yaml
    "environment variable not set" build errors.
    """
    repo_root = Path(repo_root).resolve()
    if not (repo_root / ".envrc").exists():
        return {}
    if shutil.which("direnv") is None:
        raise RuntimeError("direnv is required to capture .envrc environments but was not found on PATH")

    # Isolated, throwaway direnv state (approval database etc.), so we neither touch
    # nor depend on the user's own direnv configuration, and leave nothing behind in
    # the repo/copy.
    with tempfile.TemporaryDirectory(prefix="direnv-envcapture-") as direnv_state:
        env = os.environ.copy()
        env["DIRENV_CONFIG"] = direnv_state
        env["XDG_DATA_HOME"] = os.path.join(direnv_state, "data")
        env["DIRENV_LOG_FORMAT"] = ""

        subprocess.run(["direnv", "allow"], cwd=repo_root, env=env, capture_output=True, text=True, check=True)
        proc = subprocess.run(
            ["direnv", "export", "json"], cwd=repo_root, env=env, capture_output=True, text=True, check=False
        )
    if proc.returncode != 0:
        raise RuntimeError(f"direnv export failed for {repo_root}:\n{proc.stderr or proc.stdout}")
    changes = json.loads(proc.stdout or "{}")
    return {
        name: value
        for name, value in changes.items()
        if value is not None and not name.startswith("DIRENV_") and name not in _EXCLUDED_VAR_NAMES
    }
