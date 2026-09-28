"""What each step of an agent's work cost, worked out from the task's Codex event stream.

Codex calls each step of the agent's work an item. An item is one of:

- the user's prompt
- a reasoning block
- a message the agent wrote
- a command it ran, with the command's output
- a file edit

Each item appears in the stream as an `item.started` and an `item.completed` event.

Codex reports tokens per request, not per item. A request is one call Codex makes to the
model. This module splits every request's bill over the items that caused it. An item's
cost over the whole task has three parts:

- written: the output tokens the model spent writing it
- first send: its tokens sent at the fresh-input price in the next request
- resends: its tokens sent again from cache in every later request

A request whose cache came up short pays the fresh price for tokens that should have been
cached. The difference to the cached price is its own entry, a cache miss, because no
item caused it. What cannot be tied to an item is kept as unattributed on the request.

The parts add up to the per-request costs by construction:

- each request's output goes to the items it produced, or to the request's unattributed part
- each request's fresh input goes to what entered since the request before, the cache miss, or the unattributed part
- each request's cached tokens are shared out over the items already in the conversation, in proportion to their size, and scaled so the shares add up to exactly that request's cached tokens

The input is the list of event records the driver stored. Nothing here writes to a record.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from daml_agent_benchmark.pricing import (
    TOKEN_USAGE_EVENT_TYPES,
    RequestTokenUsage,
    attempt_events,
    cost_breakdown,
    request_token_usages,
    requests_cost,
)

# A request's cached input is usually shorter than the previous request's input in the
# same thread, even when the provider lost nothing. A thread is one conversation: the main
# agent's or one sub-agent's. The previous request ended with tokens that open the model's
# reply, which the next request replaces with the reply itself. The provider also caches
# input in fixed steps, called blocks, so the cached input is a whole number of blocks.
# The block size is the greatest common divisor of the stream's cached counts, and at least
# this many tokens. Some providers also leave the last full block uncached. So a gap of up
# to two blocks is expected, and only a larger gap is a miss.
CACHE_MISS_MIN_TOKENS = 128

CATEGORY_PROMPT = "user_message"
CATEGORY_REASONING = "reasoning"
CATEGORY_MESSAGES = "agent_message"
CATEGORY_FILE_EDITS = "file_change"
CATEGORY_COMMANDS = "command_execution"
CATEGORY_CACHE_MISS = "cache_miss"
CATEGORY_UNATTRIBUTED = "unattributed"
CATEGORIES = (
    CATEGORY_PROMPT,
    CATEGORY_REASONING,
    CATEGORY_MESSAGES,
    CATEGORY_FILE_EDITS,
    CATEGORY_COMMANDS,
    CATEGORY_CACHE_MISS,
    CATEGORY_UNATTRIBUTED,
)

# Items the model did not write, whose text reaches the next request like a tool result.
_UNWRITTEN_ITEM_TYPES = {"contextCompaction", "sub_agent_activity"}
_REASONING_TYPES = {"reasoning"}


# --- The result ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ItemCost:
    """What one completed item cost over the task. The costs are None when the model has no price."""

    tokens: float  # tokens it put into the conversation: what the model wrote plus any tool result
    written_tokens: float
    written_usd: float | None
    first_send_usd: float | None
    resends: int  # how many later requests sent it again from cache
    resend_usd: float | None
    total_usd: float | None
    category: str


@dataclass(frozen=True)
class RequestCost:
    """One request: its tokens, its cost, and the parts of it no item caused."""

    index: int  # 1-based, in stream order
    line_no: int
    sub_agent: bool  # a request of another thread, which is not split over items
    fresh: int
    cached: int
    out: int
    usd: float | None
    miss_tokens: int
    miss_usd: float | None
    unattributed_tokens: float
    unattributed_usd: float | None


@dataclass(frozen=True)
class CategoryCost:
    """The tokens and cost of one kind of item, for the summary strip."""

    key: str
    tokens: float
    usd: float | None


@dataclass(frozen=True)
class EventCosts:
    """The cost of a task split over its items, its requests and its cache misses."""

    model: str | None
    priced: bool
    total_usd: float | None
    items: dict[int, ItemCost]  # keyed by the line number of the item's `item.completed` event
    requests: list[RequestCost]
    categories: list[CategoryCost]

    def to_record(self) -> dict[str, Any]:
        """The result as JSON-ready data; the item keys become strings."""
        record = asdict(self)
        record["items"] = {str(line_no): cost for line_no, cost in record["items"].items()}
        return record


# --- Reading the stream -------------------------------------------------------------------


@dataclass(frozen=True)
class _Rates:
    """USD per token for one request. Tiered prices depend on the request's input size."""

    fresh: float
    cached: float
    output: float


_ZERO_RATES = _Rates(0.0, 0.0, 0.0)


def _rates(model: str, input_tokens: int) -> _Rates | None:
    """The per-token prices of a request with this much input; None when the model has no price."""
    total = max(2, input_tokens)
    cached = total // 2
    costs = cost_breakdown(model, total, 1_000_000, cached)
    if costs is None:
        return None
    return _Rates(costs["input"] / (total - cached), costs["cached_input"] / cached, costs["output"] / 1_000_000)


@dataclass
class _Entry:
    """Running totals for one item, or for the part of one request that no item takes.

    An item is one step of the agent's work, as the module docstring lists them.
    """

    category: str
    line_no: int | None
    reasoning: bool = False
    model_written: bool = False
    user_input: bool = False  # enters the request it precedes, not the one after
    written_weight: float = 0.0  # characters the model wrote
    tool_weight: float = 0.0  # characters a tool or the user put into the conversation
    written_tokens: float = 0.0
    tool_tokens: float = 0.0
    first_send_tokens: float = 0.0
    written_usd: float = 0.0
    first_send_usd: float = 0.0
    resend_usd: float = 0.0
    resends: int = 0

    @property
    def total_usd(self) -> float:
        return self.written_usd + self.first_send_usd + self.resend_usd

    @property
    def tokens(self) -> float:
        return self.written_tokens + self.tool_tokens

    def complete(self, completed: _Entry) -> None:
        """Take the category and sizes of the item as completed, which has its tool result."""
        self.category = completed.category
        self.line_no = completed.line_no
        self.reasoning = completed.reasoning
        self.model_written = completed.model_written
        self.user_input = completed.user_input
        self.written_weight = completed.written_weight
        self.tool_weight = completed.tool_weight

    def absorb(self, part: _Entry, fraction: float) -> None:
        """Add a fraction of another entry's tokens and costs to this one."""
        self.written_tokens += part.written_tokens * fraction
        self.tool_tokens += part.tool_tokens * fraction
        self.written_usd += part.written_usd * fraction
        self.first_send_usd += part.first_send_usd * fraction
        self.resend_usd += part.resend_usd * fraction
        self.resends = max(self.resends, part.resends)


@dataclass
class _Request:
    """One request as reported."""

    line_no: int
    sub_agent: bool
    input_tokens: int
    cached: int
    out: int
    reasoning: int
    rates: _Rates = _ZERO_RATES
    miss_tokens: int = 0
    unattributed_usd: float = 0.0
    unattributed_tokens: float = 0.0
    # Items an earlier request started that were still running when the request before it was reported.
    in_flight: list[_Entry] = field(default_factory=list)

    @property
    def fresh(self) -> int:
        return max(0, self.input_tokens - self.cached)

    @property
    def usd(self) -> float:
        return self.fresh * self.rates.fresh + self.cached * self.rates.cached + self.out * self.rates.output


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _item_entry(item: dict[str, Any], line_no: int) -> _Entry:
    """An item's category and the character counts its tokens are split by."""
    item_type = _text(item.get("type"))
    if item_type in _REASONING_TYPES:
        return _Entry(CATEGORY_REASONING, line_no, reasoning=True)
    if item_type in ("user_message", "userMessage"):
        content = item.get("content")
        parts = content if isinstance(content, list) else []
        text = "".join(_text(part.get("text")) for part in parts if isinstance(part, dict))
        return _Entry(CATEGORY_PROMPT, line_no, user_input=True, tool_weight=len(text))
    if item_type in _UNWRITTEN_ITEM_TYPES:
        return _Entry(CATEGORY_COMMANDS, line_no)
    if item_type in ("agent_message", "agentMessage"):
        return _Entry(CATEGORY_MESSAGES, line_no, model_written=True, written_weight=len(_text(item.get("text"))))
    if item_type in ("command_execution", "commandExecution"):
        command = _text(item.get("command"))
        output = _text(item.get("aggregated_output")) or _text(item.get("aggregatedOutput"))
        return _Entry(
            CATEGORY_COMMANDS,
            line_no,
            model_written=True,
            written_weight=len(command),
            tool_weight=len(output),
        )
    if item_type in ("file_change", "fileChange"):
        changes = item.get("changes")
        changes = [change for change in changes if isinstance(change, dict)] if isinstance(changes, list) else []
        return _Entry(
            CATEGORY_FILE_EDITS,
            line_no,
            model_written=True,
            written_weight=sum(len(_text(change.get("diff"))) for change in changes),
            # The tool answers with the paths it changed.
            tool_weight=sum(len(_text(change.get("path"))) for change in changes),
        )
    # Web searches, sub-agent calls and any tool Codex adds later.
    written = sum(len(_text(item.get(key))) for key in ("query", "prompt", "arguments"))
    result = sum(len(_text(item.get(key))) for key in ("result", "output", "aggregated_output"))
    return _Entry(CATEGORY_COMMANDS, line_no, model_written=True, written_weight=written, tool_weight=result)


def _main_thread(events: list[dict[str, Any]]) -> str | None:
    """The thread the task's own agent runs on: the first one started, or the first to report usage."""
    for event in events:
        if event.get("type") == "thread.started" and isinstance(event.get("thread"), dict):
            return _text(event["thread"].get("id")) or None
    for event in events:
        if event.get("type") in TOKEN_USAGE_EVENT_TYPES:
            return _text(event.get("thread_id")) or None
    return None


def _request(request: RequestTokenUsage, *, sub_agent: bool) -> _Request:
    return _Request(
        request.line_no,
        sub_agent,
        request.input_tokens,
        request.cached_input_tokens,
        request.output_tokens,
        request.reasoning_output_tokens,
    )


# --- Splitting ----------------------------------------------------------------------------


def _share(entries: list[_Entry], weights: list[float], amount: float) -> list[tuple[_Entry, float]]:
    """`amount` split over the entries in proportion to the weights, or evenly when they are all 0."""
    total = sum(weights)
    if total > 0:
        return [(entry, amount * weight / total) for entry, weight in zip(entries, weights, strict=True)]
    return [(entry, amount / len(entries)) for entry in entries]


def _assign_output(request: _Request, produced: list[_Entry], unattributed: _Entry) -> None:
    """A request's output tokens to the items it wrote.

    - The reasoning tokens go to the reasoning items, evenly.
    - The rest goes to the other items the model wrote, by the characters it wrote.
    - When one of the two groups is missing, the other takes all of it.
    - When there is neither, the output stays on the request as unattributed.
    """
    reasoning = [entry for entry in produced if entry.reasoning]
    written = [entry for entry in produced if entry.model_written]
    reasoning_tokens = min(request.reasoning, request.out)
    shares: list[tuple[_Entry, float]] = []
    if reasoning and written:
        shares += _share(reasoning, [0.0] * len(reasoning), reasoning_tokens)
        rest = request.out - reasoning_tokens
        shares += _share(written, [e.written_weight for e in written], rest)
    elif reasoning:
        shares += _share(reasoning, [0.0] * len(reasoning), request.out)
    elif written:
        shares += _share(written, [e.written_weight for e in written], request.out)
    else:
        shares.append((unattributed, request.out))
    for entry, tokens in shares:
        entry.written_tokens += tokens
        entry.written_usd += tokens * request.rates.output


def _add_first_send(entry: _Entry, tokens: float, rate: float) -> None:
    entry.first_send_tokens += tokens
    entry.first_send_usd += tokens * rate


def _assign_first_send(request: _Request, fresh: float, entering: list[_Entry], unattributed: _Entry) -> None:
    """A request's fresh input, less any cache miss, to what entered the conversation since the request before.

    - What the model wrote in the previous request comes first, scaled down if the fresh input is smaller.
    - The rest is tool results and user messages, split over the items that have one by its characters.
    - Without such an item it goes to the other tool calls evenly, then to any entering item by what it wrote.
    - Without any item it stays on the previous request as unattributed.
    """
    rate = request.rates.fresh
    writers = [entry for entry in [*entering, unattributed] if entry.written_tokens > 0]
    written_total = sum(entry.written_tokens for entry in writers)
    scale = min(1.0, fresh / written_total) if written_total > 0 else 0.0
    for entry in writers:
        _add_first_send(entry, entry.written_tokens * scale, rate)
    rest = fresh - written_total * scale
    if rest <= 0:
        return
    with_result = [entry for entry in entering if entry.tool_weight > 0]
    tool_calls = [entry for entry in entering if not entry.reasoning and entry.category != CATEGORY_MESSAGES]
    if with_result:
        targets, weights = with_result, [entry.tool_weight for entry in with_result]
    elif tool_calls:
        targets, weights = tool_calls, [0.0] * len(tool_calls)
    elif entering:
        targets, weights = entering, [entry.written_tokens for entry in entering]
    else:
        targets, weights = [unattributed], [1.0]
    for entry, tokens in _share(targets, weights, rest):
        entry.tool_tokens += tokens
        _add_first_send(entry, tokens, rate)


def _first_send_from_cache(request: _Request, tokens: float, entering: list[_Entry]) -> None:
    """Cached tokens of a request with nothing earlier to resend, as on the first request.

    They count as part of the first send of what is entering now, at the cached price.
    """
    targets = (
        [entry for entry in entering if entry.first_send_tokens > 0]
        or [entry for entry in entering if entry.line_no is not None]
        or entering[-1:]
    )
    for entry, share in _share(targets, [e.first_send_tokens for e in targets], tokens):
        entry.tool_tokens += share
        _add_first_send(entry, share, request.rates.cached)


def _miss_tokens(previous: _Request | None, request: _Request, block: int) -> int:
    """Tokens the request paid fresh that the previous request of its thread had already sent.

    A request whose input shrank had its context compacted, so a short cache is expected.
    """
    if previous is None or request.input_tokens < previous.input_tokens:
        return 0
    deficit = previous.input_tokens - request.cached
    return deficit if deficit > 2 * block else 0


@dataclass
class _Stream:
    """The requests and items of one task's event stream."""

    # The usage events that report a request, by line number. The others are repeats.
    reported: dict[int, RequestTokenUsage]
    requests: list[_Request]  # every request in stream order
    main_requests: list[_Request]
    # produced[i]: the items main request i + 1 produced. The last list holds items of a request not reported yet.
    produced: list[list[_Entry]]


def _walk_stream(records: list[dict[str, Any]]) -> _Stream:
    """Group the stream's items by the request that produced them.

    Requests of other threads are kept as sub-agent requests.
    Each main request notes the items still running when the request before it was reported.
    """
    events = attempt_events(records)
    main_thread = _main_thread([event for _, event in events])
    stream = _Stream({request.line_no: request for request in request_token_usages(events)}, [], [], [[]])
    running: dict[str, _Entry] = {}  # items started and not completed yet, by item id
    running_at_span_start: list[_Entry] = []
    for line_no, event in events:
        thread_id = _text(event.get("thread_id")) or None
        if event.get("type") in TOKEN_USAGE_EVENT_TYPES and line_no not in stream.reported:
            continue
        reported_request = stream.reported.get(line_no)
        if thread_id is not None and main_thread is not None and thread_id != main_thread:
            if reported_request is not None:
                stream.requests.append(_request(reported_request, sub_agent=True))
            continue
        item = event.get("item")
        if reported_request is not None:
            request = _request(reported_request, sub_agent=False)
            request.in_flight = running_at_span_start
            stream.requests.append(request)
            stream.main_requests.append(request)
            stream.produced.append([])
            running_at_span_start = list(running.values())
        elif event.get("type") == "item.started" and isinstance(item, dict):
            # Keyed by the start line until the item completes, so a running item shows a cost too.
            entry = _item_entry(item, line_no)
            running[_text(item.get("id"))] = entry
            stream.produced[-1].append(entry)
        elif event.get("type") == "item.completed" and isinstance(item, dict):
            entry = running.pop(_text(item.get("id")), None)
            if entry is None:
                stream.produced[-1].append(_item_entry(item, line_no))
            else:
                entry.complete(_item_entry(item, line_no))
    return stream


def _split_requests(stream: _Stream, model: str | None) -> list[_Entry]:
    """Split each main request's cost over its items.

    Each request gets its rates when a priced model is given.
    Each item gets its output, first send and resends.
    Each request gets its cache miss.
    The result holds what no item takes, indexed by request.
    """
    main_requests = stream.main_requests
    if model is not None:
        for request in stream.requests:
            request.rates = _rates(model, request.input_tokens) or _ZERO_RATES
    block = max(CACHE_MISS_MIN_TOKENS, math.gcd(*(request.cached for request in stream.requests)))
    previous_of_thread: dict[str | None, _Request] = {}
    for request in stream.requests:
        # Main requests share one key, as they include any request without a thread id.
        thread = stream.reported[request.line_no].thread_id if request.sub_agent else None
        request.miss_tokens = _miss_tokens(previous_of_thread.get(thread), request, block)
        previous_of_thread[thread] = request

    # unattributed[k]: what main request k wrote that no item takes; unattributed[0] is what entered request 1 without an item.
    unattributed = [_Entry(CATEGORY_UNATTRIBUTED, None) for _ in stream.produced]
    # entering[k]: what request k sent first. That is the previous request's items and output, and this request's user messages.
    entering: list[list[_Entry]] = [[] for _ in range(len(stream.produced) + 2)]
    for index, items in enumerate(stream.produced):
        for entry in items:
            entering[index + 1 if entry.user_input else index + 2].append(entry)

    # A request's cached input, and its cache miss at the cached price, is spread over the
    # entries already in the conversation, each by the tokens it entered with. An entry
    # sent first by request e is resent from request e + 1 on. Request k stores its price per token
    # of that pool, so an entry's resend cost is its size times a suffix sum.
    pool_size = 0.0
    # per_pool_token[k]: request k's cached price per token of the pool; a suffix sum once the loop is done.
    per_pool_token = [0.0] * (len(main_requests) + 2)
    # resent[k]: 1 when request k resent the pool; after the suffix sum, how many requests from k on did.
    resent = [0] * (len(main_requests) + 2)
    for k, request in enumerate(main_requests, start=1):
        _assign_first_send(request, request.fresh - request.miss_tokens, entering[k], unattributed[k - 1])
        if k >= 2:
            pool_size += sum(entry.first_send_tokens for entry in (*entering[k - 1], unattributed[k - 2]))
        from_cache = request.cached + request.miss_tokens
        if from_cache > 0 and pool_size > 0:
            per_pool_token[k] = from_cache * request.rates.cached / pool_size
            resent[k] = 1
        elif from_cache > 0:
            _first_send_from_cache(request, from_cache, [*entering[k], unattributed[k - 1]])
        _assign_output(request, stream.produced[k - 1], unattributed[k])
    for index in range(len(main_requests), 0, -1):
        per_pool_token[index] += per_pool_token[index + 1]
        resent[index] += resent[index + 1]
    for first_request in range(1, len(entering)):
        for entry in (*entering[first_request], *unattributed[first_request - 1 : first_request]):
            joined = min(first_request + 1, len(main_requests) + 1)
            if entry.first_send_tokens > 0:
                entry.resend_usd = entry.first_send_tokens * per_pool_token[joined]
                entry.resends = resent[joined]
    return unattributed


def _settle_leftovers(stream: _Stream, unattributed: list[_Entry]) -> None:
    """Put what no item took on the item or request it belongs to.

    A polling request's leftover goes to the commands still running.
    The rest shows as unattributed on its request.
    A sub-agent request is unattributed as a whole, less its cache miss.
    """
    main_requests = stream.main_requests
    for k, request in enumerate(main_requests, start=1):
        polled = request.in_flight and not any(e.reasoning or e.model_written for e in stream.produced[k - 1])
        if polled:
            for entry in request.in_flight:
                entry.absorb(unattributed[k], 1 / len(request.in_flight))
            unattributed[k] = _Entry(CATEGORY_UNATTRIBUTED, None)
    # What request k wrote without an item shows on request k; what entered request 1 without an item on request 1.
    for index, entry in enumerate(unattributed):
        if main_requests:
            owner = main_requests[max(index, 1) - 1]
            owner.unattributed_tokens += entry.tokens
            owner.unattributed_usd += entry.total_usd
    for request in stream.requests:
        if request.sub_agent:
            request.unattributed_tokens = request.fresh + request.cached + request.out - request.miss_tokens
            request.unattributed_usd = request.usd - request.miss_tokens * (request.rates.fresh - request.rates.cached)


def _result(stream: _Stream, model: str | None, priced: bool) -> EventCosts:
    """The per-item, per-request and per-category costs, and the task's total.

    Every cost is None when the model has no price.
    """

    def usd(value: float) -> float | None:
        return value if priced else None

    items = {
        entry.line_no: ItemCost(
            tokens=entry.tokens,
            written_tokens=entry.written_tokens,
            written_usd=usd(entry.written_usd),
            first_send_usd=usd(entry.first_send_usd),
            resends=entry.resends,
            resend_usd=usd(entry.resend_usd),
            total_usd=usd(entry.total_usd),
            category=entry.category,
        )
        for items_of_request in stream.produced
        for entry in items_of_request
    }
    request_costs = [
        RequestCost(
            index=index,
            line_no=request.line_no,
            sub_agent=request.sub_agent,
            fresh=request.fresh,
            cached=request.cached,
            out=request.out,
            usd=usd(request.usd),
            miss_tokens=request.miss_tokens,
            miss_usd=usd(request.miss_tokens * (request.rates.fresh - request.rates.cached)),
            unattributed_tokens=request.unattributed_tokens,
            unattributed_usd=usd(request.unattributed_usd),
        )
        for index, request in enumerate(stream.requests, start=1)
    ]
    tokens_by_category = dict.fromkeys(CATEGORIES, 0.0)
    usd_by_category = dict.fromkeys(CATEGORIES, 0.0)
    for items_of_request in stream.produced:
        for entry in items_of_request:
            tokens_by_category[entry.category] += entry.tokens
            usd_by_category[entry.category] += entry.total_usd
    for request in stream.requests:
        tokens_by_category[CATEGORY_CACHE_MISS] += request.miss_tokens
        usd_by_category[CATEGORY_CACHE_MISS] += request.miss_tokens * (request.rates.fresh - request.rates.cached)
        tokens_by_category[CATEGORY_UNATTRIBUTED] += request.unattributed_tokens
        usd_by_category[CATEGORY_UNATTRIBUTED] += request.unattributed_usd
    categories = [CategoryCost(key, tokens_by_category[key], usd(usd_by_category[key])) for key in CATEGORIES]
    # The same sum the runner records for the task, so the two agree to the last digit.
    total = requests_cost(model, stream.reported.values()) if model is not None else None
    return EventCosts(
        model=model,
        priced=priced,
        total_usd=total.total if total is not None else None,
        items=items,
        requests=request_costs,
        categories=categories,
    )


def event_costs(records: list[dict[str, Any]], model: str | None) -> EventCosts:
    """The task's cost split over its events, from the event records the driver stored.

    `model` prices the requests. When it is None or has no known price, the token figures are
    still worked out and every cost is None.

    The records are in stream order. Codex reports a request's usage after the items the
    request produced, and after the tool calls it made have run. An item therefore belongs
    to the request whose usage event follows the item's `item.started` event. A command
    that outlives that usage event still belongs to the request that issued it.

    A request that produced no item while a command was still running was the model polling
    that command. What such a request cost goes to the running command.
    """
    priced = model is not None and _rates(model, 0) is not None
    stream = _walk_stream(records)
    unattributed = _split_requests(stream, model if priced else None)
    _settle_leftovers(stream, unattributed)
    return _result(stream, model, priced)
