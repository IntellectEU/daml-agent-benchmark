#!/bin/bash
# canton-specific SDK store contents.
#
# canton's daml.yaml pins an SDK snapshot that exists only as a GitHub release —
# it was never published to the dpm registry, so `dpm install` cannot fetch it.
# Install it with the classic assistant instead (note: the download is named
# after the release TAG, which for snapshots differs from the SDK VERSION inside
# it), then register it in dpm's cache by hand so `dpm build` can resolve it
# (see bootstrap_dpm_cache.py for how that registration works).
#
# canton containers also need DAML_VERSION set to this snapshot version — that is
# injected per-container by the harness (the canton handler's
# container_env), not baked here.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The canton handler (repos/canton.py) passes both, so the versions live in one place.
: "${CANTON_TAG:?CANTON_TAG is set by the canton handler}"
: "${CANTON_VER:?CANTON_VER is set by the canton handler}"

if [ ! -d "/opt/daml/sdk/${CANTON_TAG}" ]; then
    echo "Installing canton snapshot SDK ${CANTON_TAG}"
    /opt/daml/bin/daml install "${CANTON_TAG}" --install-assistant no
    chmod -R a+rwX "/opt/daml/sdk/${CANTON_TAG}"
fi

if [ ! -f "/opt/dpm/cache/sdk/open-source/${CANTON_VER}.yaml" ]; then
    python3 "${SCRIPT_DIR}/bootstrap_dpm_cache.py" \
        --dpm-home /opt/dpm \
        --classic-sdk "/opt/daml/sdk/${CANTON_TAG}" \
        --version "${CANTON_VER}"
fi
