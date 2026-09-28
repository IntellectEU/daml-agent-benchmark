"""Tests for pricing a task per request.

Some models are priced by the size of each request: past an input size, every token costs
more. The tests check that:

- each request is priced on its own, so two requests of 200,000 input tokens cost less than
  one of 400,000 when the model has such a size tier
- the attempt's cost from a usage stream is the sum of its requests priced one by one, and so are its parts
- a repeated usage report is not a new request, and a sub-agent thread's requests count too
"""

import pytest

from daml_agent_benchmark.pricing import (
    RequestTokenUsage,
    attempt_events,
    cost_breakdown,
    request_token_usages,
    requests_cost,
)
from daml_agent_benchmark.records import TokenUsage
from daml_agent_benchmark.task_run.app_server import usage_cost

MODEL = "gpt-6-luna"


def _price(input_tokens: int, output_tokens: int = 0, cached: int = 0) -> float:
    return sum(cost_breakdown(MODEL, input_tokens, output_tokens, cached).values())


def _request(input_tokens: int, output_tokens: int = 0, cached: int = 0, thread_id: str = "main") -> RequestTokenUsage:
    return RequestTokenUsage(0, thread_id, input_tokens, cached, output_tokens, 0)


def test_each_request_is_priced_at_its_own_tier() -> None:
    cost = requests_cost(MODEL, [_request(200_000), _request(200_000)])
    assert cost.total == pytest.approx(2 * _price(200_000))
    if _price(400_000) / 400_000 <= _price(200_000) / 200_000:
        pytest.skip(f"the price list has no size tier for {MODEL} between 200,000 and 400,000 input tokens")
    # Priced as one request, the same tokens would reach the higher tier.
    assert cost.total < _price(400_000)


def test_parts_are_per_request_sums() -> None:
    cost = requests_cost(MODEL, [_request(150_000, 2_000, 100_000), _request(180_000, 3_000, 150_000)])
    first = cost_breakdown(MODEL, 150_000, 2_000, 100_000)
    second = cost_breakdown(MODEL, 180_000, 3_000, 150_000)
    assert cost.input == pytest.approx(first["input"] + second["input"])
    assert cost.cached_input == pytest.approx(first["cached_input"] + second["cached_input"])
    assert cost.output == pytest.approx(first["output"] + second["output"])
    assert cost.total == cost.input + cost.cached_input + cost.output


def test_a_model_without_a_price_has_no_cost() -> None:
    assert requests_cost("no-such-model-anywhere", [_request(1_000)]) is None
    assert requests_cost("no-such-model-anywhere", []) is None


def _usage_event(thread_id: str, last: tuple[int, int, int], total: tuple[int, int, int]) -> dict:
    def counts(values: tuple[int, int, int]) -> dict:
        return {"inputTokens": values[0], "cachedInputTokens": values[1], "outputTokens": values[2]}

    return {
        "type": "thread.token_usage.updated",
        "thread_id": thread_id,
        "token_usage": {"last": counts(last), "total": counts(total)},
    }


def _records(events: list[dict]) -> list[dict]:
    return [{"line_no": index + 1, "event": event} for index, event in enumerate(events)]


def test_attempt_cost_is_the_sum_of_its_requests() -> None:
    records = _records(
        [
            {"type": "thread.started", "thread": {"id": "main"}},
            _usage_event("main", (200_000, 150_000, 1_000), (200_000, 150_000, 1_000)),
            _usage_event("helper", (30_000, 0, 500), (30_000, 0, 500)),
            _usage_event("main", (210_000, 199_000, 2_000), (410_000, 349_000, 3_000)),
            # An interrupted turn repeats the last report; the running total has not moved.
            _usage_event("main", (210_000, 199_000, 2_000), (410_000, 349_000, 3_000)),
            _usage_event("helper", (35_000, 29_000, 400), (65_000, 29_000, 900)),
        ]
    )
    requests = request_token_usages(attempt_events(records))
    assert [(request.thread_id, request.input_tokens) for request in requests] == [
        ("main", 200_000),
        ("helper", 30_000),
        ("main", 210_000),
        ("helper", 35_000),
    ]
    expected = _price(200_000, 1_000, 150_000) + _price(30_000, 500) + _price(210_000, 2_000, 199_000) + _price(35_000, 400, 29_000)
    cost = usage_cost(MODEL, TokenUsage(475_000, 3_900, 378_000), records)
    assert cost.total == pytest.approx(expected)
    assert cost.input + cost.cached_input + cost.output == cost.total


def test_attempt_without_usage_has_no_cost() -> None:
    assert usage_cost(MODEL, None, []) is None


def test_only_the_last_attempt_of_a_restarted_stream_counts() -> None:
    first = _records([_usage_event("main", (1_000, 0, 10), (1_000, 0, 10))])
    second = _records([{"type": "thread.started", "thread": {"id": "main"}}, _usage_event("main", (2_000, 0, 20), (2_000, 0, 20))])
    requests = request_token_usages(attempt_events([*first, *second]))
    assert [request.input_tokens for request in requests] == [2_000]


def test_usage_without_request_reports_cannot_be_priced() -> None:
    """Tokens with no per-request usage to price them by give no cost, not a cost of zero."""
    assert usage_cost(MODEL, TokenUsage(1000, 100, 200), []) is None
