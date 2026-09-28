# DAML SDK only provides x86_64 Linux builds, so force amd64 even on ARM hosts.
FROM --platform=linux/amd64 node:20-bookworm-slim

SHELL ["/bin/bash", "-lc"]

# Avoid tput/ncurses errors in non-interactive builds
ENV TERM=dumb

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        direnv \
        git \
        jq \
        make \
        nix-bin \
        openjdk-17-jre-headless \
        python3 \
        python3-pip \
        tar \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Pinned deliberately: the agent version is part of what a benchmark result means,
# so it must not drift between machines or rebuilds. Moving the pin needs a live run
# through the runner first (identity, usage, egress and grading fields all checked),
# since event shapes and config keys change between releases.
ARG CODEX_VERSION=0.153.4
RUN npm install --global "@openai/codex@${CODEX_VERSION}"

# ---------------------------------------------------------------------------
# Daml SDKs and DPM are NOT baked into this image. They are static data (~20GB
# across all benchmark SDK versions), so baking them made the image enormous and
# every rebuild re-download them. Instead they live in a host-side "SDK store"
# (see bootstrap_sdk_store.sh) that containers mount read-only at the paths
# below. The image itself stays small and rebuilds in minutes.
# ---------------------------------------------------------------------------
ENV DAML_HOME=/opt/daml
ENV DPM_HOME=/opt/dpm
ENV PATH="/opt/dpm/bin:/opt/daml/bin:${PATH}"
# Login shells reset PATH from /etc/profile; persist both tools there too.
RUN echo 'export PATH="/opt/daml/bin:$PATH"' >> /etc/profile.d/daml.sh \
    && printf 'export DPM_HOME=/opt/dpm\nexport PATH="/opt/dpm/bin:$PATH"\n' > /etc/profile.d/dpm.sh
# Mount points for the SDK store (empty in the image itself). /opt/daml is the
# read-only daml mount; the dpm store mounts at /opt/dpm-store, and each container
# assembles its private /opt/dpm from it at startup (see setup_dpm_home.sh for why).
# /opt/dpm is world-writable because containers run as the host user's uid.
COPY setup_dpm_home.sh /opt/setup_dpm_home.sh
RUN mkdir -p /opt/daml /opt/dpm /opt/dpm-store && chmod 0777 /opt/dpm && chmod 0755 /opt/setup_dpm_home.sh

# The container runs as the host user's UID/GID (non-root), so /workspace needs
# world-writable permissions so that docker cp can write sandbox files into it
# and codex can create/modify files during the run, regardless of which UID is used.
RUN mkdir -p /workspace && chmod 0777 /workspace
WORKDIR /workspace
