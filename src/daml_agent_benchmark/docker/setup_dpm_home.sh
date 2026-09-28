#!/bin/bash
# Assemble a container-local DPM_HOME (/opt/dpm) from the read-only SDK store
# mounted at /opt/dpm-store.
#
# Why not mount the store at /opt/dpm directly? dpm opens its SDK manifest files
# read-write even when it only reads them, so a read-only mount makes every build
# fail with "read-only file system" — while a writable mount would let one task's
# agent poison the SDKs that other tasks use. The middle ground assembled here:
# the tiny manifest files are COPIED into the container (so dpm's read-write opens how
# succeed, and any writes stay private to this container), while the multi-GB
# component payloads are symlinks into the read-only mount (fast, and tamper-proof
# at the filesystem level).
#
# Idempotent; safe to call more than once per container.
set -euo pipefail

if [ ! -d /opt/dpm-store ] || [ -e /opt/dpm/bin ]; then
    exit 0
fi

mkdir -p /opt/dpm/cache
ln -s /opt/dpm-store/bin /opt/dpm/bin
for entry in /opt/dpm-store/cache/*; do
    name=$(basename "${entry}")
    if [ "${name}" = "sdk" ]; then
        cp -r "${entry}" /opt/dpm/cache/sdk
        chmod -R u+w /opt/dpm/cache/sdk 2>/dev/null || true
    else
        ln -s "${entry}" "/opt/dpm/cache/${name}"
    fi
done
# Any other top-level store entries (config files etc.): expose read-only via symlink.
for entry in /opt/dpm-store/*; do
    name=$(basename "${entry}")
    [ -e "/opt/dpm/${name}" ] || ln -s "${entry}" "/opt/dpm/${name}"
done
