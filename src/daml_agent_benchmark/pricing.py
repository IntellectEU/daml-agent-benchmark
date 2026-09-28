"""Cost of a run from the token counts that codex reports.

Prices come from the genai-prices package, which finds the provider from the model name.

The provider prices each request on its own. Some prices are tiered by the size of
one request, so a task's summed tokens priced as one request would land in a tier that
none of its requests reached. A task's cost is therefore the sum of its requests' costs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

TOKEN_USAGE_EVENT_TYPES = frozenset({"thread.token_usage.updated"})


def cost_breakdown(
    model_name: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0
) -> dict[str, float] | None:
    """The cost of one request's token usage split into fresh input, cached input and output.

    `input_tokens` includes the cached ones. Each part is in USD under the keys `input`,
    `cached_input` and `output`. The result is None for a model the price list does not know.
    """
    from genai_prices import Usage, calc_price
    from genai_prices.types import calc_unit_price

    usage = Usage(
        input_tokens=max(0, int(input_tokens)),
        cache_read_tokens=max(0, int(cached_input_tokens)),
        output_tokens=max(0, int(output_tokens)),
    )
    try:
        price = calc_price(usage, model_ref=model_name)
    except LookupError:
        return None
    model_price = price.model_price
    total_input = usage.input_tokens
    fresh_input = max(0, total_input - usage.cache_read_tokens)
    return {
        "input": float(calc_unit_price(model_price.input_mtok, fresh_input, total_input, 1_000_000)),
        "cached_input": float(calc_unit_price(model_price.cache_read_mtok, usage.cache_read_tokens, total_input, 1_000_000)),
        "output": float(price.output_price),
    }


# --- Requests from the event stream -------------------------------------------------------


@dataclass(frozen=True)
class RequestTokenUsage:
    """The token counts of one request Codex sent to the model API.

    They come from one `thread.token_usage.updated` event. Codex repeats the last report
    when a turn is interrupted. Those repeats are dropped, so there is one per request.
    """

    line_no: int
    thread_id: str | None
    input_tokens: int  # includes the cached ones
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int


def attempt_events(records: Iterable[dict[str, Any]]) -> list[tuple[int, dict[str, Any]]]:
    """The (line number, event) pairs of the last attempt in a list of stored event records.

    A stream that restarts its line numbers holds several attempts; only the last one counts.
    """
    events = [(record["line_no"], record["event"]) for record in records if isinstance(record.get("event"), dict)]
    restarts = [index for index in range(1, len(events)) if events[index][0] <= events[index - 1][0]]
    return events[restarts[-1] :] if restarts else events


def _int(value: Any) -> int:
    """A token count from a raw event, or 0 when the field is missing or not a number."""
    return int(value) if isinstance(value, (int, float)) else 0


def request_token_usages(events: Iterable[tuple[int, dict[str, Any]]]) -> list[RequestTokenUsage]:
    """The token usage of every request the events report, in stream order, over all threads.

    Each usage event carries the tokens of the one request it follows under `last`. Codex
    repeats the last usage report when a turn is interrupted. A report whose running total
    has not moved since the thread's previous report is that repeat, not a new request.
    """
    requests = []
    last_totals: dict[str | None, Any] = {}
    for line_no, event in events:
        if event.get("type") not in TOKEN_USAGE_EVENT_TYPES:
            continue
        usage = event.get("token_usage")
        if not isinstance(usage, dict) or not isinstance(usage.get("last"), dict):
            continue
        thread_id = event.get("thread_id") if isinstance(event.get("thread_id"), str) else None
        total = usage.get("total")
        if total is not None and last_totals.get(thread_id) == total:
            continue
        last_totals[thread_id] = total
        last = usage["last"]
        requests.append(
            RequestTokenUsage(
                line_no=line_no,
                thread_id=thread_id,
                input_tokens=_int(last.get("inputTokens")),
                cached_input_tokens=_int(last.get("cachedInputTokens")),
                output_tokens=_int(last.get("outputTokens")),
                reasoning_output_tokens=_int(last.get("reasoningOutputTokens")),
            )
        )
    return requests


# --- Pricing the requests -----------------------------------------------------------------


@dataclass(frozen=True)
class RequestsCost:
    """What a list of requests cost, each request priced alone. Every part is a sum over the requests."""

    input: float
    cached_input: float
    output: float

    @property
    def total(self) -> float:
        return self.input + self.cached_input + self.output


def requests_cost(model: str, requests: Iterable[RequestTokenUsage]) -> RequestsCost | None:
    """The cost of the requests, each priced alone; None when the model has no price."""
    fresh = cached = output = 0.0
    for request in requests:
        parts = cost_breakdown(model, request.input_tokens, request.output_tokens, request.cached_input_tokens)
        if parts is None:
            return None
        fresh += parts["input"]
        cached += parts["cached_input"]
        output += parts["output"]
    if cost_breakdown(model, 0, 0) is None:
        return None
    return RequestsCost(fresh, cached, output)
