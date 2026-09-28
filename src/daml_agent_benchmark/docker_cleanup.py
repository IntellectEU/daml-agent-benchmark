from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone

RUNNER_DOCKER_LABEL = "com.daml-agent-benchmark.runner"
RUNNER_DOCKER_LABEL_VALUE = "container-agent-eval"


def _parse_docker_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return None

    iso_match = re.fullmatch(r"(.+?)(\.\d+)?Z", text)
    if iso_match:
        base = iso_match.group(1)
        fraction = iso_match.group(2) or ""
        if fraction:
            fraction = "." + fraction[1:7].ljust(6, "0")
        return datetime.fromisoformat(f"{base}{fraction}+00:00")

    spaced_match = re.fullmatch(r"(.+?)(\.\d+)? ([+-]\d{4}) UTC", text)
    if spaced_match:
        base = spaced_match.group(1)
        fraction = spaced_match.group(2) or ".000000"
        offset = spaced_match.group(3)
        fraction = "." + fraction[1:7].ljust(6, "0")
        return datetime.strptime(f"{base}{fraction} {offset}", "%Y-%m-%d %H:%M:%S.%f %z")

    return datetime.fromisoformat(text)


def _is_older_than(timestamp: datetime, now: datetime, threshold_seconds: int) -> bool:
    return (now - timestamp.astimezone(timezone.utc)).total_seconds() >= threshold_seconds


def _stale_container_reference_time(status: str, created_at: str, finished_at: str) -> datetime | None:
    if status in {"exited", "dead"}:
        finished = _parse_docker_timestamp(finished_at)
        if finished is not None:
            return finished
    return _parse_docker_timestamp(created_at)


def _split_docker_table_line(line: str, expected_fields: int) -> list[str]:
    parts = line.split("\t")
    if len(parts) != expected_fields:
        raise ValueError(f"Unexpected docker output field count ({len(parts)} != {expected_fields}): {line!r}")
    return parts


def _container_ids(docker_bin: str) -> list[str]:
    proc = subprocess.run(
        [docker_bin, "ps", "-aq"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to list Docker containers for stale cleanup: {stderr}")
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def _stale_daml_container_ids(
    *,
    docker_bin: str,
    container_image: str,
    egress_proxy_image: str,
    now: datetime,
    stale_after_seconds: int,
) -> list[str]:
    ids = _container_ids(docker_bin)
    if not ids:
        return []
    proc = subprocess.run(
        [
            docker_bin,
            "inspect",
            "--format",
            "{{.Id}}\t{{.Name}}\t{{.Config.Image}}\t{{.State.Status}}\t{{.Created}}\t{{.State.FinishedAt}}",
            *ids,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to inspect Docker containers for stale cleanup: {stderr}")

    stale_ids: list[str] = []
    for line in (proc.stdout or "").splitlines():
        container_id, name, config_image, status, created_at, finished_at = _split_docker_table_line(line, 6)
        if status not in {"created", "exited", "dead"}:
            continue
        if config_image not in {container_image, egress_proxy_image} and not name.startswith("/daml-agent-egress-"):
            continue
        reference_time = _stale_container_reference_time(status, created_at, finished_at)
        if reference_time is None:
            continue
        if _is_older_than(reference_time, now, stale_after_seconds):
            stale_ids.append(container_id)
    return stale_ids


def _stale_daml_network_names(*, docker_bin: str, now: datetime, stale_after_seconds: int) -> list[str]:
    list_proc = subprocess.run(
        [docker_bin, "network", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if list_proc.returncode != 0:
        stderr = (list_proc.stderr or list_proc.stdout or "").strip()
        raise RuntimeError(f"Failed to list Docker networks for stale cleanup: {stderr}")
    names = [line.strip() for line in (list_proc.stdout or "").splitlines() if line.startswith("daml-agent-int-")]
    if not names:
        return []

    inspect_proc = subprocess.run(
        [
            docker_bin,
            "network",
            "inspect",
            "--format",
            "{{.Name}}\t{{.Created}}\t{{json .Containers}}",
            *names,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect_proc.returncode != 0:
        stderr = (inspect_proc.stderr or inspect_proc.stdout or "").strip()
        raise RuntimeError(f"Failed to inspect Docker networks for stale cleanup: {stderr}")

    stale_names: list[str] = []
    for line in (inspect_proc.stdout or "").splitlines():
        name, created_at, containers_json = _split_docker_table_line(line, 3)
        if containers_json != "{}":
            continue
        created = _parse_docker_timestamp(created_at)
        if created is not None and _is_older_than(created, now, stale_after_seconds):
            stale_names.append(name)
    return stale_names


def cleanup_stale_daml_docker_resources(
    *,
    docker_bin: str,
    container_image: str,
    egress_proxy_image: str,
    enabled: bool,
    stale_after_seconds: int,
) -> None:
    if not enabled:
        return
    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be > 0")

    now = datetime.now(timezone.utc)
    ids = _stale_daml_container_ids(
        docker_bin=docker_bin,
        container_image=container_image,
        egress_proxy_image=egress_proxy_image,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )
    networks = _stale_daml_network_names(
        docker_bin=docker_bin,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )

    if ids:
        rm_proc = subprocess.run(
            [docker_bin, "rm", "-f", *ids],
            capture_output=True,
            text=True,
            check=False,
        )
        if rm_proc.returncode != 0:
            stderr = (rm_proc.stderr or rm_proc.stdout or "").strip()
            raise RuntimeError(f"Failed to remove stale DAML Docker containers: {stderr}")

    if networks:
        network_rm_proc = subprocess.run(
            [docker_bin, "network", "rm", *networks],
            capture_output=True,
            text=True,
            check=False,
        )
        if network_rm_proc.returncode != 0:
            stderr = (network_rm_proc.stderr or network_rm_proc.stdout or "").strip()
            raise RuntimeError(f"Failed to remove stale DAML Docker networks: {stderr}")

    if ids or networks:
        age_hours = stale_after_seconds / 3600
        print(
            f"[container] removed stale Docker resources older than {age_hours:g}h: "
            f"containers={len(ids)}, networks={len(networks)}",
            flush=True,
        )

    _prune_stale_builder_cache(docker_bin=docker_bin)


# Keep the most-recently-used build cache (the current image's layers, so per-run
# rebuilds stay cache-hits) and prune everything older. Orphaned layer generations
# from interrupted/dev builds otherwise accumulate without bound and can fill the
# Docker VM disk. Budget sized against a host with only ~20GB free for Docker
# overall: the image plus this cache must fit in that.
_BUILDER_CACHE_KEEP_STORAGE = "20GB"


def _prune_stale_builder_cache(*, docker_bin: str) -> None:
    """Prune docker build cache beyond the keep-storage budget (LRU order)."""
    proc = subprocess.run(
        [docker_bin, "builder", "prune", "-f", f"--keep-storage={_BUILDER_CACHE_KEEP_STORAGE}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Cache pruning is hygiene, not a run prerequisite — warn and continue.
        stderr = (proc.stderr or proc.stdout or "").strip()
        print(f"[container] WARNING: builder cache prune failed: {stderr}", flush=True)
        return
    reclaimed = next((line for line in (proc.stdout or "").splitlines() if "reclaimed" in line.lower()), "")
    if reclaimed:
        print(f"[container] builder cache prune: {reclaimed.strip()}", flush=True)
