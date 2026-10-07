"""Tests for splitting a task's cost over its events.

The tests build a small Codex event stream of five requests, in the order Codex reports
them: each request's items come before its usage event. The fifth request finds nothing
in the cache. The tests check that:

- the item costs, the cache miss and the unattributed parts add up to the per-request costs
- the command that read three files gets the tokens of what it read, and costs the most
- the cache miss is priced at the difference between the fresh and the cached price
- a model without a price gets token figures and no costs
- a request whose input shrank is not a cache miss, and the costs still add up
- a sub-agent's request is checked against the sub-agent's previous request
- a gap of up to two cache blocks is not a cache miss, and a larger one is
"""

import pytest

from daml_agent_benchmark.pricing import cost_breakdown
from daml_agent_benchmark.server.event_costs import event_costs

MODEL = "gpt-6-luna"
READ_FILES = "/usr/bin/bash -lc \"sed -n '1,240p' Move.daml && sed -n '1,260p' Path.daml && sed -n '1,320p' Rules.daml\""


def _item(item_type: str, item_id: str, **fields) -> dict:
    return {"type": item_type, "id": item_id, **fields}


def _usage(input_tokens: int, cached: int, out: int, reasoning: int, total: int, thread: str = "main") -> dict:
    last = {"inputTokens": input_tokens, "cachedInputTokens": cached, "outputTokens": out, "reasoningOutputTokens": reasoning}
    return {
        "type": "thread.token_usage.updated",
        "thread_id": thread,
        "token_usage": {"last": last, "total": {"totalTokens": total}},
    }


def _stream(requests: list[tuple[list[dict], dict]]) -> list[dict]:
    """Event records for requests given as (items the request produced, its usage event)."""
    events: list[dict] = [{"type": "thread.started", "thread": {"id": "main"}}]
    for items, usage in requests:
        for item in items:
            events.append({"type": "item.started", "item": item, "thread_id": "main"})
            events.append({"type": "item.completed", "item": item, "thread_id": "main"})
        events.append(usage)
    return [{"line_no": index + 1, "event": event} for index, event in enumerate(events)]


def _toy_requests(*, request5_cached: int = 0, request3_input: int = 18_850) -> list[tuple[list[dict], dict]]:
    prompt = _item("user_message", "u1", content=[{"type": "text", "text": "Make Rules.daml pass. " * 40}])
    return [
        (
            [
                prompt,
                _item("reasoning", "r1", summary=[], content=[]),
                _item("agent_message", "m1", text="I will read the rule files and the test first."),
                _item("command_execution", "c1", command=READ_FILES, aggregated_output="x" * 36_000),
            ],
            _usage(6_000, 0, 150, 30, 1),
        ),
        (
            [
                _item("reasoning", "r2", summary=[], content=[]),
                _item("command_execution", "c2", command="/usr/bin/bash -lc 'daml build'", aggregated_output="w" * 14_000),
            ],
            _usage(15_150, 6_000, 200, 120, 2),
        ),
        (
            [
                _item("reasoning", "r3", summary=[], content=[]),
                _item("file_change", "f1", changes=[{"path": "/workspace/Move.daml", "diff": "+fix\n" * 60}]),
            ],
            _usage(request3_input, min(15_150, request3_input - 1_000), 900, 600, 3),
        ),
        (
            [_item("command_execution", "c3", command="/usr/bin/bash -lc 'daml test'", aggregated_output="t" * 8_000)],
            _usage(19_850, min(18_850, request3_input), 100, 0, 4),
        ),
        ([_item("agent_message", "m2", text="Fixed the path check; all tests pass.")], _usage(21_950, request5_cached, 150, 0, 5)),
    ]


def _rates() -> tuple[float, float, float]:
    """Fresh, cached and output price per token of the test model."""
    costs = cost_breakdown(MODEL, 2_000, 1_000_000, 1_000)
    return costs.input / 1_000, costs.cached_input / 1_000, costs.output / 1_000_000


def _line_of(records: list[dict], item_id: str) -> int:
    return next(r["line_no"] for r in records if r["event"]["type"] == "item.completed" and r["event"]["item"]["id"] == item_id)


def _assert_reconciles(result) -> None:
    parts = sum(item.total_usd for item in result.items.values())
    parts += sum(request.miss_usd + request.unattributed_usd for request in result.requests)
    requests = sum(request.usd for request in result.requests)
    assert parts == pytest.approx(requests, abs=1e-12)
    assert result.total_usd == pytest.approx(requests, abs=1e-12)
    assert sum(category.usd for category in result.categories) == pytest.approx(requests, abs=1e-12)
    billed = sum(cost_breakdown(MODEL, c.fresh + c.cached, c.out, c.cached).total for c in result.requests)
    assert requests == pytest.approx(billed, rel=1e-9)


def test_costs_add_up_to_the_requests() -> None:
    result = event_costs(_stream(_toy_requests()), MODEL)
    assert result.priced
    assert [request.index for request in result.requests] == [1, 2, 3, 4, 5]
    _assert_reconciles(result)


def test_the_file_read_gets_what_it_read_and_costs_the_most() -> None:
    records = _stream(_toy_requests())
    result = event_costs(records, MODEL)
    read = result.items[_line_of(records, "c1")]
    assert read.category == "command_execution"
    # Request 2's fresh input is request 1's output plus what the command read.
    assert read.tokens - read.written_tokens == pytest.approx(15_150 - 6_000 - 150)
    assert read.resends == 3
    assert max(result.items.values(), key=lambda item: item.total_usd) is read
    prompt = result.items[_line_of(records, "u1")]
    assert prompt.category == "user_message"
    assert prompt.tokens == pytest.approx(6_000)
    assert result.items[_line_of(records, "c2")].category == "command_execution"


def test_cache_miss_is_its_own_entry() -> None:
    result = event_costs(_stream(_toy_requests()), MODEL)
    fresh, cached, _ = _rates()
    misses = [request for request in result.requests if request.miss_tokens]
    assert [request.index for request in misses] == [5]
    assert misses[0].miss_tokens == 19_850
    assert misses[0].miss_usd == pytest.approx(19_850 * (fresh - cached), rel=1e-12)
    category = next(c for c in result.categories if c.key == "cache_miss")
    assert category.usd == pytest.approx(misses[0].miss_usd, rel=1e-12)


def test_a_model_without_a_price_gets_tokens_only() -> None:
    records = _stream(_toy_requests())
    result = event_costs(records, "no-such-model-anywhere")
    assert not result.priced
    assert result.total_usd is None
    assert all(request.usd is None and request.miss_usd is None for request in result.requests)
    read = result.items[_line_of(records, "c1")]
    assert read.total_usd is None and read.written_usd is None
    assert read.tokens > 9_000


def test_compaction_is_not_a_miss() -> None:
    # Request 3's input shrinks below request 2's, as after compacting the context.
    result = event_costs(_stream(_toy_requests(request5_cached=19_850, request3_input=12_000)), MODEL)
    assert all(request.miss_tokens == 0 for request in result.requests)
    _assert_reconciles(result)


def test_a_sub_agent_miss_is_counted() -> None:
    # A sub-agent's second request finds nothing in the cache, while the main thread keeps its cache.
    requests = _toy_requests(request5_cached=19_850)
    requests.insert(2, ([], _usage(5_000, 0, 100, 0, 1, thread="sub")))
    requests.insert(4, ([], _usage(7_000, 0, 100, 0, 2, thread="sub")))
    result = event_costs(_stream(requests), MODEL)
    misses = [(request.index, request.sub_agent, request.miss_tokens) for request in result.requests if request.miss_tokens]
    assert misses == [(5, True, 5_000)]
    _assert_reconciles(result)


def _block_requests() -> list[tuple[list[dict], dict]]:
    """Four requests whose cached counts are whole numbers of 1,024-token blocks."""
    sizes = [(3_000, 0), (5_000, 2_048), (7_000, 3_072), (9_000, 2_048)]
    return [
        ([_item("agent_message", f"m{index}", text="Next step.")], _usage(input_tokens, cached, 100, 0, index))
        for index, (input_tokens, cached) in enumerate(sizes, start=1)
    ]


def test_a_gap_of_up_to_two_blocks_is_not_a_miss() -> None:
    # Requests 2 and 3 get 952 and 1,928 tokens less from cache than the request before sent.
    result = event_costs(_stream(_block_requests()), MODEL)
    assert [request.miss_tokens for request in result.requests[:3]] == [0, 0, 0]


def test_a_large_drop_is_still_a_miss() -> None:
    # Request 4's cache falls back to two blocks, 4,952 tokens short of what request 3 sent.
    result = event_costs(_stream(_block_requests()), MODEL)
    assert result.requests[3].miss_tokens == 7_000 - 2_048
    _assert_reconciles(result)
