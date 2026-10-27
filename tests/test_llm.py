"""agents/llm.py: FakeLLM replay, cost metering, budget breaker, bounded retries."""
from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from agents.llm import LLM, Budget, BudgetExceeded, FakeLLM, LLMError, Meter, cost_usd, text, tool_use

SYSTEM = [{"type": "text", "text": "sys"}]


def test_fake_replays_in_order_and_records_requests():
    llm = FakeLLM([{"content": [text("one")]}, {"content": [tool_use("get_order", {"order_id": "1"}, id="t1")]}])
    meter = Meter()
    a = llm.create(SYSTEM, [{"role": "user", "content": "hi"}], [], meter=meter, tag="x")
    b = llm.create(SYSTEM, [{"role": "user", "content": "hi"}], [], meter=meter, tag="x")
    assert a.text() == "one" and b.tool_uses()[0]["id"] == "t1"
    assert meter.calls == 2 and len(llm.requests) == 2
    with pytest.raises(AssertionError):
        llm.create(SYSTEM, [], [])


def test_cost_uses_all_four_token_kinds():
    pricing = {"m": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2}}
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_write_tokens": 400_000, "cache_read_tokens": 1_000_000}
    assert cost_usd("m", usage, pricing) == pytest.approx(2.0 + 1.0 + 1.0 + 0.2)


def test_budget_stops_at_85_percent(tmp_path):
    budget = Budget(cap_usd=1.0, ledger=tmp_path / "ledger.jsonl")
    llm = FakeLLM(lambda req: {"content": [text("x")]}, budget=budget)
    llm.pricing = {"fake": {"input": 0, "output": 1_000_000 * 0.30 / 50, "cache_write": 0, "cache_read": 0}}  # $0.30 per call
    for _ in range(3):
        llm.create(SYSTEM, [{"role": "user", "content": "hi"}], [])
    assert budget.spent == pytest.approx(0.90)
    with pytest.raises(BudgetExceeded):
        llm.create(SYSTEM, [{"role": "user", "content": "hi"}], [])
    # the ledger carries the spend across processes
    assert Budget(cap_usd=1.0, ledger=tmp_path / "ledger.jsonl").spent == pytest.approx(0.90)


def _status_error(cls, code):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


class _Client:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=100, cache_read_input_tokens=0)
        block = SimpleNamespace(model_dump=lambda exclude_none=True: {"type": "text", "text": "ok"})
        return SimpleNamespace(usage=usage, content=[block], stop_reason="end_turn")


def _llm(client, **kw):
    return LLM(model="claude-sonnet-5-5", budget=Budget(cap_usd=10, ledger=None), client=client, sleep=lambda s: None, **kw)


def test_retries_429_and_5xx_then_succeeds():
    client = _Client([_status_error(anthropic.RateLimitError, 429), _status_error(anthropic.InternalServerError, 500)])
    meter = Meter()
    r = _llm(client).create(SYSTEM, [{"role": "user", "content": "hi"}], [], meter=meter)
    assert r.text() == "ok" and client.calls == 3
    assert meter.cache_write_tokens == 100 and meter.tokens_in == 110


def test_retries_are_bounded():
    client = _Client([_status_error(anthropic.RateLimitError, 429)] * 10)
    with pytest.raises(LLMError):
        _llm(client, max_retries=2).create(SYSTEM, [{"role": "user", "content": "hi"}], [])
    assert client.calls == 3


def test_400_is_not_retried():
    client = _Client([_status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(LLMError):
        _llm(client).create(SYSTEM, [{"role": "user", "content": "hi"}], [])
    assert client.calls == 1
