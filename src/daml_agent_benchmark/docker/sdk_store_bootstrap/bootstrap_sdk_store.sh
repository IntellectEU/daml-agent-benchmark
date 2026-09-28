#!/bin/bash
# Populate the host-side SDK store. Runs INSIDE the daml-agent-eval container with
# the store mounted read-write at /opt/daml and /opt/dpm (see sdk_store.py, which
# launches this) and network access enabled.
#
# The store holds everything version-shaped that benchmark containers need:
#   /opt/daml   the classic daml assistant + one subdir per SDK version
#   /opt/dpm    the dpm binary + its component cache
# Benchmark containers mount both read-only, so this script is the only thing
# that ever writes them. Every step checks whether its output already exists,
# so re-running with a longer SDK list only installs what is missing.
#
# This file holds only the GENERIC steps. Repo-specific SDK needs (canton's
# GitHub-only snapshot) live in the repo_*.sh files next to it, which this driver
# runs at the end, plus any mounted at /bootstrap-scripts-extra by extra code with more
# repositories. This mirrors how each repository's own build fix-ups live in its handler.
#
# Arguments: SDK versions to install, e.g. `bootstrap_sdk_store.sh 1.16.0 3.4.9`.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- 1. The classic daml assistant itself (needed to install everything else). ---
# The get.daml.com script breaks in non-interactive Docker (missing TERM, GitHub
# rate limits), so fetch the latest version string and download directly.
if [ ! -x /opt/daml/bin/daml ]; then
    VERSION=$(curl -fsSL https://docs.daml.com/latest)
    URL="https://github.com/digital-asset/daml/releases/download/v${VERSION}/daml-sdk-${VERSION}-linux.tar.gz"
    echo "Installing daml assistant ${VERSION}"
    curl -fSL "${URL}" -o /tmp/daml-sdk.tar.gz
    mkdir -p /tmp/daml-sdk
    tar xzf /tmp/daml-sdk.tar.gz -C /tmp/daml-sdk --strip-components=1
    # install.sh honors DAML_HOME (=/opt/daml in the image), so it installs
    # straight into the mounted store. Older SDK installers ignore it and use
    # ~/.daml instead — move the result over in that case.
    /tmp/daml-sdk/install.sh
    if [ ! -x /opt/daml/bin/daml ] && [ -d /root/.daml ]; then
        cp -a /root/.daml/. /opt/daml/
    fi
    rm -rf /tmp/daml-sdk /tmp/daml-sdk.tar.gz
fi

# --- 2. The SDK versions the benchmark tasks need. ---
for sdk_version in "$@"; do
    if [ ! -d "/opt/daml/sdk/${sdk_version}" ]; then
        echo "Installing Daml SDK ${sdk_version}"
        /opt/daml/bin/daml install "${sdk_version}"
        chmod -R a+rwX "/opt/daml/sdk/${sdk_version}"
    fi
done

# --- 3. dpm (the successor tool; some repos build with it instead of daml). ---
if [ ! -x /opt/dpm/bin/dpm ]; then
    echo "Installing dpm"
    curl -fsSL https://get.digitalasset.com/install/install.sh | sh
fi

# --- 4. Repo-specific store contents. ---
# ALL repository scripts run on every bootstrap — there is no per-task selectivity.
# The store is deliberately a shared superset of everything the benchmark can
# need, built once rather than assembled per run. Each script checks whether its
# outputs already exist, so on a warm store these are near-instant no-ops (and
# the bootstrap container is only launched at all when something is missing —
# see ensure_sdk_store). Trade-off: a fresh store downloads every repo's SDKs
# even if you only want one repo's tasks; if that ever matters, pass the needed
# repository set in and filter here.
for repo_script in "${SCRIPT_DIR}"/repo_*.sh /bootstrap-scripts-extra/repo_*.sh; do
    [ -e "${repo_script}" ] || continue
    echo "Running $(basename "${repo_script}")"
    bash "${repo_script}"
done

# Containers run as the host user's (non-root) uid; make everything readable.
chmod -R a+rwX /opt/dpm
echo "SDK store ready: $(ls /opt/daml/sdk | tr '\n' ' ')"
