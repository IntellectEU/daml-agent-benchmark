"""Squid access-log parsing and per-task attribution for the egress audit."""

from daml_agent_benchmark.egress_audit import (
    domain_from_request_url,
    host_from_request_url,
    parse_squid_access_line,
    parse_squid_access_log_text,
    summarize_egress_events,
)

IP_TUNNEL_SERVED = (
    "1782921123.000    120 10.219.0.18 TCP_TUNNEL/200 700 CONNECT 93.184.216.34:443 - HIER_DIRECT/93.184.216.34 -"
)
IP_HTTP_DENIED = (
    "1782921124.000      0 10.219.0.18 TCP_DENIED/403 3894 GET http://10.219.0.1:22/ - HIER_NONE/- text/html"
)

ALLOWED_TUNNEL = (
    "1782921119.123    412 10.219.0.18 TCP_TUNNEL/200 5120 CONNECT api.openai.com:443 - HIER_DIRECT/104.18.7.192 -"
)
DENIED_TUNNEL = "1782921120.456      0 10.219.0.18 TCP_DENIED/403 3894 CONNECT github.com:443 - HIER_NONE/- text/html"
DENIED_HTTP = (
    "1782921121.789      0 10.219.0.34 TCP_DENIED/403 3894 GET http://example.com/index.html - HIER_NONE/- text/html"
)
OTHER_TASK = "1782921122.000    300 10.219.1.5 TCP_TUNNEL/200 900 CONNECT chatgpt.com:443 - HIER_DIRECT/1.2.3.4 -"


def test_domain_from_request_url_handles_connect_and_http_forms() -> None:
    assert domain_from_request_url("api.openai.com:443") == "api.openai.com"
    assert domain_from_request_url("http://example.com/index.html") == "example.com"
    assert domain_from_request_url("https://user@Docs.Daml.com:8443/x") == "docs.daml.com"
    assert domain_from_request_url("[::1]:443") == ""
    assert domain_from_request_url("not a url") == ""
    assert domain_from_request_url("93.184.216.34:443") == ""
    assert host_from_request_url("93.184.216.34:443") == "93.184.216.34"
    assert host_from_request_url("[::1]:443") == "::1"
    assert host_from_request_url("http://10.219.0.1:22/") == "10.219.0.1"


def test_ip_literal_hosts_are_counted() -> None:
    events = parse_squid_access_log_text("\n".join([ALLOWED_TUNNEL, IP_TUNNEL_SERVED, IP_HTTP_DENIED]))
    assert [e["host"] for e in events] == ["api.openai.com", "93.184.216.34", "10.219.0.1"]
    assert [e["domain"] for e in events] == ["api.openai.com", "", ""]
    summary = summarize_egress_events(events, ["openai.com"])
    # An IP literal can never match the domain allowlist: served means the allowlist failed.
    assert summary["egress_non_allowed_domains"] == ["93.184.216.34"]
    assert summary["egress_non_allowed_access_detected"] is True
    assert summary["egress_blocked_domains"] == ["10.219.0.1"]
    assert summary["egress_blocked_attempts_detected"] is True


def test_parse_squid_access_line_classifies_blocked() -> None:
    allowed = parse_squid_access_line(ALLOWED_TUNNEL)
    assert allowed is not None
    assert allowed["client"] == "10.219.0.18"
    assert allowed["domain"] == "api.openai.com"
    assert allowed["method"] == "CONNECT"
    assert allowed["http_status"] == 200
    assert allowed["blocked"] is False

    denied = parse_squid_access_line(DENIED_TUNNEL)
    assert denied is not None
    assert denied["domain"] == "github.com"
    assert denied["blocked"] is True

    assert parse_squid_access_line("garbage line") is None


def test_parse_log_filters_by_client_subnet() -> None:
    text = "\n".join([ALLOWED_TUNNEL, DENIED_TUNNEL, DENIED_HTTP, OTHER_TASK])
    task_events = parse_squid_access_log_text(text, client_subnet="10.219.0.16/28")
    assert [e["domain"] for e in task_events] == ["api.openai.com", "github.com"]
    all_events = parse_squid_access_log_text(text)
    assert len(all_events) == 4


def test_summarize_separates_blocked_from_served_non_allowed() -> None:
    events = parse_squid_access_log_text("\n".join([ALLOWED_TUNNEL, DENIED_TUNNEL, DENIED_HTTP, OTHER_TASK]))
    summary = summarize_egress_events(events, ["openai.com", "oaiusercontent.com"])
    assert summary["egress_domains_observed"] == ["api.openai.com", "chatgpt.com", "example.com", "github.com"]
    assert summary["egress_blocked_domains"] == ["example.com", "github.com"]
    # chatgpt.com was tunnelled but is not on the allowlist: this is the allowlist-bug signal.
    assert summary["egress_non_allowed_domains"] == ["chatgpt.com"]
    assert summary["egress_non_allowed_access_detected"] is True
    assert summary["egress_blocked_attempts_detected"] is True


def test_summarize_clean_run() -> None:
    summary = summarize_egress_events(parse_squid_access_log_text(ALLOWED_TUNNEL), ["openai.com"])
    assert summary["egress_non_allowed_access_detected"] is False
    assert summary["egress_blocked_attempts_detected"] is False
    assert summary["egress_domains_observed"] == ["api.openai.com"]
