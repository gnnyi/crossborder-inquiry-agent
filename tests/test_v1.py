"""V1 workflow: code-owned fetch, order-number clean-up in the workflow (not the sandbox), fixed edges."""
from __future__ import annotations

import pytest

from agents.common import check_output, get_message
from agents.llm import FakeLLM, Meter, text, tool_use
from agents.v1 import normalize_order_ref, run_v1
from sandbox.tools import Sandbox

MSG = get_message("m003")


@pytest.mark.parametrize("raw,want", [("#10231", "10231"), ("NW-10231", "10231"), ("nw10231", "10231"),
                                      ("1O231", "10231"), (10231, "10231"), (" 10231 ", "10231"), ("no number", None)])
def test_normalize_order_ref(raw, want):
    assert normalize_order_ref(raw) == want


def test_sandbox_still_rejects_what_v1_cleans():
    assert Sandbox().get_order("#10231")["status"] == "invalid_argument"


def _classify(**over):
    c = {"intents": ["WISMO"], "priority": "P1", "risk_flags": [], "order_refs": ["#10231"], "skus": [], "needs": ["order", "tracking"]}
    c.update(over)
    return {"content": [tool_use("classify", c)]}


def _draft(claims=(), note=None):
    return {"content": [tool_use("submit_draft", {"reply": {"text": "Hi. Northwren Care", "claims": list(claims)}, "handoff_note": note})]}


def test_fetch_is_decided_by_code_from_classification():
    claim = {"text": "Status shipped.", "source": {"type": "tool", "call_id": "c1", "field": "status"}}
    sb = Sandbox()
    out, trace = run_v1(MSG, FakeLLM([_classify(), _draft([claim])]), sb, Meter())
    assert [(r["tool"], r["args"], r["status"]) for r in sb.log] == [
        ("get_order", {"order_id": "10231"}, "ok"), ("get_tracking", {"order_id": "10231"}, "ok")]
    assert check_output(out, sb.log) == [] and out["route"] == "AUTO_REPLY"
    assert [t.get("node") for t in trace if t["type"] == "model"] == ["v1.classify", "v1.draft"]


def test_route_comes_from_flags_and_note_is_filled():
    out, _ = run_v1(MSG, FakeLLM([_classify(risk_flags=["IDENTITY_MISMATCH"]), _draft()]), Sandbox(), Meter())
    assert out["route"] == "ESCALATE" and out["handoff_note"]


def test_email_lookup_only_follows_up_when_unambiguous():
    msg = get_message("m002")      # sender with orders on file, no order number in the text
    sb = Sandbox()
    run_v1(msg, FakeLLM([_classify(order_refs=[], needs=["orders_by_email", "order"]), _draft()]), sb, Meter())
    assert sb.log[0]["tool"] == "find_orders_by_email"
    n_orders = len(sb.log[0]["result"]["orders"]) if sb.log[0]["status"] == "ok" else 0
    assert len(sb.log) == (2 if n_orders == 1 else 1)


def test_classify_failure_hands_off_and_skips_lookups():
    sb = Sandbox()
    out, _ = run_v1(MSG, FakeLLM([{"content": [text("hmm")]}, {"content": [text("still no")]}]), sb, Meter())
    assert out["meta"]["handoff_reason"] == "classify_failed" and sb.log == []


def test_bad_draft_gets_one_retry():
    ghost = {"text": "x", "source": {"type": "tool", "call_id": "c99", "field": "status"}}
    out, trace = run_v1(MSG, FakeLLM([_classify(), _draft([ghost]), _draft()]), Sandbox(), Meter())
    assert not out["meta"].get("fallback")
    drafts = [t for t in trace if t.get("node") == "v1.draft"]
    assert len(drafts) == 2 and drafts[0]["errors"]


def test_bad_draft_twice_hands_off():
    ghost = {"text": "x", "source": {"type": "tool", "call_id": "c99", "field": "status"}}
    out, _ = run_v1(MSG, FakeLLM([_classify(), _draft([ghost]), _draft([ghost])]), Sandbox(), Meter())
    assert out["meta"]["handoff_reason"] == "draft_contract_invalid" and out["route"] == "ESCALATE"
