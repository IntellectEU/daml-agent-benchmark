"""Parse and classify squid access-log lines from the benchmark egress proxy.

The egress proxy is the only route out of a task container. Its access log is
therefore the ground truth for "what did the agent try to reach": every request
appears there, whether squid tunnelled it or denied it. The same parser serves
the per-task audit (the container wrapper filters the shared log down to its own
task's client subnet) and the run-level audit (the orchestrator reads the whole
log at teardown), so the two views cannot disagree on what counts as blocked.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Collection

# Squid's default `squid` logformat:
#   %ts.%03tu %6tr %>a %Ss/%03>Hs %<st %rm %ru %[un %Sh/%<a %mt
# i.e. timestamp, duration, client address, action/status, bytes, method, url, ...
_MIN_FIELDS = 7
_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,63}$")


def normalize_domain(domain: str) -> str:
    value = (domain or "").strip().lower().strip(".")
    if value.startswith("www."):
        value = value[4:]
    return value


def normalize_host(host: str) -> str:
    """An exact host as squid and the audit compare it: lower case, with no leading or trailing dot."""
    return (host or "").strip().lower().strip(".")


def domain_is_allowed(domain: str, allowed_suffixes: list[str], allowed_hosts: Collection[str] = ()) -> bool:
    """Whether the proxy allows `domain`.

    It does when `domain` is one of `allowed_suffixes` or a subdomain of one. It also
    does when `domain` is exactly one of `allowed_hosts`. A subdomain of an allowed
    host is not allowed.
    """
    if normalize_host(domain) in {normalize_host(h) for h in allowed_hosts if normalize_host(h)}:
        return True
    normalized = normalize_domain(domain)
    for suffix in allowed_suffixes:
        allowed = normalize_domain(suffix)
        if not allowed:
            continue
        if normalized == allowed or normalized.endswith("." + allowed):
            return True
    return False


def host_from_request_url(url: str) -> str:
    """Extract the host from a squid-logged request URL: a domain, an IPv4 literal, or a bracketed IPv6 literal.

    Squid logs `host:443` for CONNECT and the full URL otherwise. Returns "" when
    no host can be recognised.
    """
    value = str(url or "").strip()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", value, flags=re.IGNORECASE)
    value = value.split("/", 1)[0]
    value = value.rsplit("@", 1)[-1]
    if value.startswith("["):
        literal = value[1:].split("]", 1)[0]
        try:
            return str(ipaddress.ip_address(literal))
        except ValueError:
            return ""
    value = value.split(":", 1)[0].strip(".").lower()
    if _DOMAIN_RE.match(value):
        return value
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def domain_from_request_url(url: str) -> str:
    """The host of a request URL when it is a domain name; "" for IP literals and unrecognised hosts."""
    host = host_from_request_url(url)
    return host if _DOMAIN_RE.match(host) else ""


def parse_squid_access_line(line: str) -> dict | None:
    """Return one egress event for a squid access-log line, or None for unparsable lines.

    `blocked` is True when squid refused the request (TCP_DENIED or an HTTP 403
    result). A blocked event is evidence of an attempt, not of a leak. `host` is
    whatever the request named (domain or IP literal); `domain` is set only when
    that is a domain name.
    """
    parts = line.split()
    if len(parts) < _MIN_FIELDS:
        return None
    action_status = parts[3]
    action, _, status_text = action_status.partition("/")
    try:
        http_status = int(status_text)
    except ValueError:
        http_status = None
    host = host_from_request_url(parts[6])
    blocked = "DENIED" in action.upper() or http_status == 403
    try:
        timestamp = float(parts[0])
    except ValueError:
        timestamp = None
    return {
        "timestamp": timestamp,
        "client": parts[2],
        "action": action,
        "http_status": http_status,
        "method": parts[5],
        "url": parts[6],
        "host": host,
        "domain": host if _DOMAIN_RE.match(host) else "",
        "blocked": blocked,
    }


def parse_squid_access_log_text(text: str, *, client_subnet: str | None = None) -> list[dict]:
    """Parse a whole access log, optionally keeping only events from one client subnet."""
    network = ipaddress.ip_network(client_subnet, strict=False) if client_subnet else None
    events: list[dict] = []
    for line in (text or "").splitlines():
        event = parse_squid_access_line(line)
        if event is None:
            continue
        if network is not None:
            try:
                if ipaddress.ip_address(event["client"]) not in network:
                    continue
            except ValueError:
                continue
        events.append(event)
    return events


def summarize_egress_events(events: list[dict], allowed_suffixes: list[str], allowed_hosts: Collection[str] = ()) -> dict:
    """Split observed hosts into allowed, blocked-forbidden and forbidden-but-served.

    The allowlist is read as `domain_is_allowed` reads it: `allowed_suffixes` cover
    their subdomains, and `allowed_hosts` match exactly.

    `egress_non_allowed_domains` must always be empty: it means a host outside the
    allowlist got through the proxy, which is an allowlist bug, not agent behaviour.
    `egress_blocked_domains` records what the agent attempted and squid refused.
    Hosts are domains or IP literals; only a domain can match the allowlist, so a
    served IP literal always counts as non-allowed.
    """
    hosts_observed: set[str] = set()
    blocked_hosts: set[str] = set()
    non_allowed_served: set[str] = set()
    for event in events:
        host = event["host"]
        if not host:
            continue
        hosts_observed.add(host)
        allowed = bool(event["domain"]) and domain_is_allowed(host, allowed_suffixes, allowed_hosts)
        if event["blocked"]:
            blocked_hosts.add(host)
        elif not allowed:
            non_allowed_served.add(host)
    return {
        "egress_events": events,
        "egress_domains_observed": sorted(hosts_observed),
        "egress_blocked_domains": sorted(blocked_hosts),
        "egress_non_allowed_domains": sorted(non_allowed_served),
        "egress_non_allowed_access_detected": bool(non_allowed_served),
        "egress_blocked_attempts_detected": bool(blocked_hosts),
    }


def squid_config_text(port: str, allowed_domains: list[str], allowed_hosts: Collection[str] = ()) -> str:
    """Squid configuration for the egress allowlist proxy.

    The proxy tunnels HTTPS to port 443 and refuses everything else, plain HTTP included.

    Squid's `dstdomain` reads a leading dot as "this domain and its subdomains", and a
    name without one as "this host only". Each of `allowed_domains` gets the dot, so it
    covers its subdomains. Each of `allowed_hosts` goes in without it, so it matches
    exactly. The two lists are separate ACLs, because squid warns when one ACL holds
    both a domain and a host under it.

    `buffer-size=0KB` makes squid write each access-log line as the request
    completes; with the default buffering, lines only appear when the buffer fills
    or squid exits, and a per-task audit that reads the log at task end would miss
    that task's own requests. Squid requires the unit suffix.
    """
    hosts = list(dict.fromkeys(normalize_host(h) for h in allowed_hosts if normalize_host(h)))
    lines = [
        f"http_port {port}",
        "cache deny all",
        "access_log stdio:/var/log/squid/access.log buffer-size=0KB",
        "acl SSL_ports port 443",
        "acl CONNECT method CONNECT",
        "acl allowed_domains dstdomain " + " ".join(f".{d}" for d in allowed_domains if d),
    ]
    if hosts:
        lines.append("acl allowed_hosts dstdomain " + " ".join(hosts))
    lines += [
        "http_access deny !CONNECT",
        "http_access deny !SSL_ports",
        "http_access allow allowed_domains",
    ]
    if hosts:
        lines.append("http_access allow allowed_hosts")
    lines.append("http_access deny all")
    return "\n".join(lines) + "\n"
