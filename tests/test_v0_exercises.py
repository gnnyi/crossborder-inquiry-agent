"""Acceptance tests for the 5 hands-on changes in docs/V0_walkthrough.md section 9.

They are skipped until you do the exercise: delete the skip marker of one test, make it pass, keep the
rest of the suite green. The tests describe behaviour, not implementation, so any reasonable design passes.
"""
from __future__ import annotations

import json

import pytest

from agents.common import get_message
from agents.llm import FakeLLM, Meter, tool_use
from agents.v0 import Limits, agent_loop, run_v0
from sandbox.tools import Sandbox

TODO = pytest.mark.skip(reason="exercise for the reader: docs/V0_walkthrough.md section 9")


def _submit():
    return {"content": [tool_use("submit_response", {"intents": ["WISMO"], "priority": "P1", "risk_flags": [],
                                                     "handoff_note": None, "reply": {"text": "Hi", "claims": []}})]}


@TODO
def test_ex1_timeout_retry_backs_off():
    """Exercise 1: wait base * 2**attempt seconds between timeout retries (0.5s, 1.0s, ...)."""
    slept = []
    calls = []

    def run_tool(name, args):
        calls.append(1)
        return {"call_id": f"c{len(calls)}", "status": "timeout" if len(calls) < 3 else "ok", "result": {}, "error": None}

    limits = Limits(max_timeout_retries=2, retry_backoff_s=0.5)              # new field
    agent_loop(FakeLLM([{"content": [tool_use("get_order", {"order_id": "1"})]}, _submit()]), [], "hi",
               [{"name": "get_order"}], run_tool, lambda s: [], limits, Meter(), "t", sleep=slept.append)  # new kwarg
    assert slept == [0.5, 1.0]


@TODO
def test_ex2_stop_on_token_budget():
    """Exercise 2: hand off with reason "token_budget" once the message's prompt tokens exceed a cap."""
    big = {"input_tokens": 30_000, "output_tokens": 100, "cache_write_tokens": 0, "cache_read_tokens": 0}
    looping = lambda req: {"content": [tool_use("get_order", {"order_id": "10231"})], "usage": big}
    out, _ = run_v0(get_message("m003"), FakeLLM(looping), Sandbox(), Meter(),
                    limits=Limits(max_steps=50, max_tool_calls=50, max_input_tokens=100_000))   # new field
    assert out["meta"]["handoff_reason"] == "token_budget"
    assert out["meta"]["tokens_in"] <= 130_000


@TODO
def test_ex3_not_found_order_suggests_email_lookup():
    """Exercise 3: when get_order returns not_found, the tool result tells the model it can look the
    customer up by email instead. The order number itself is still not repaired by code."""
    llm = FakeLLM([{"content": [tool_use("get_order", {"order_id": "99999"})]}, _submit()])
    run_v0(get_message("m003"), llm, Sandbox(), Meter())
    sent = llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert json.loads(sent)["status"] == "not_found"
    assert "find_orders_by_email" in sent


@TODO
def test_ex4_identical_call_is_served_from_memory():
    """Exercise 4: an identical tool call that already returned ok is answered from the earlier result
    (same call_id), without a second sandbox call."""
    sb = Sandbox()
    script = [{"content": [tool_use("get_order", {"order_id": "10231"})]},
              {"content": [tool_use("get_order", {"order_id": "10231"})]}, _submit()]
    run_v0(get_message("m003"), FakeLLM(script), sb, Meter())
    assert len(sb.log) == 1


@TODO
def test_ex5_handoff_note_says_what_was_tried():
    """Exercise 5: when the loop gives up, handoff_note lists the tools called and their statuses, so the
    human does not start from zero."""
    looping = lambda req: {"content": [tool_use("get_order", {"order_id": "10231"})]}
    out, _ = run_v0(get_message("m003"), FakeLLM(looping), Sandbox(), Meter(), limits=Limits(max_steps=2))
    assert "get_order" in out["handoff_note"] and "ok" in out["handoff_note"]
