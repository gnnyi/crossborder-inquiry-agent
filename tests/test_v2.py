"""V2 orchestrator-worker: isolated worker contexts, explicit brief/findings, code-resolved evidence for review."""
from __future__ import annotations

import json

from agents.common import check_output, get_message
from agents.llm import FakeLLM, Meter, text, tool_use
from agents.v2 import run_v2
from sandbox.tools import Sandbox

MSG = get_message("m003")
BRIEF = {"task": "Look up order 10231", "context": "order 10231", "questions": ["status?"]}
TOOL_SRC = {"type": "tool", "call_id": "c1", "field": "status"}


def script_for(review_verdict="pass", worker_source=TOOL_SRC, final_source=TOOL_SRC):
    """Routes on the request's tag: orchestrator, orders worker, review worker."""
    orch = iter([
        {"content": [tool_use("delegate", {"worker": "orders", "brief": BRIEF})]},
        {"content": [tool_use("delegate", {"worker": "review", "brief": {"task": "review", "context": "", "questions": []},
                                           "draft": {"text": "Your order shipped.", "claims": [{"text": "Shipped.", "source": final_source}]}})]},
        {"content": [tool_use("submit_response", {"intents": ["WISMO"], "priority": "P1", "risk_flags": [], "handoff_note": None,
                                                  "reply": {"text": "Hi. Northwren Care", "claims": [{"text": "Shipped.", "source": final_source}]}})]},
    ])
    orders = iter([
        {"content": [tool_use("get_order", {"order_id": "10231"})]},
        {"content": [tool_use("submit_findings", {"findings": [{"statement": "status", "source": worker_source}], "unresolved": [], "notes": ""})]},
        {"content": [tool_use("submit_findings", {"findings": [], "unresolved": ["gave up"], "notes": ""})]},
    ])
    review = iter([{"content": [tool_use("submit_review", {"verdict": review_verdict, "issues": []})]}])

    def script(req):
        return next({"v2.orchestrator": orch, "v2.orders": orders, "v2.review": review}[req["tag"]])
    return script


def test_happy_path_and_v2_stats():
    llm, sb = FakeLLM(script_for()), Sandbox()
    out, trace = run_v2(MSG, llm, sb, Meter())
    assert check_output(out, sb.log) == []
    v2 = out["meta"]["v2"]
    assert v2["delegations"] == {"orders": 1, "review": 1} and v2["reviewed"] and v2["claims_not_from_findings"] == 0
    assert out["meta"]["llm_calls"] == 6


def test_workers_do_not_see_the_conversation():
    llm = FakeLLM(script_for())
    run_v2(MSG, llm, Sandbox(), Meter())
    customer_text = MSG["thread"][0]["text"][:40]
    worker_reqs = [r for r in llm.requests if r["tag"] != "v2.orchestrator"]
    assert worker_reqs and all(customer_text not in json.dumps(r["messages"]) for r in worker_reqs)
    orch = next(r for r in llm.requests if r["tag"] == "v2.orchestrator")
    assert "<knowledge_base>" not in orch["system"][0]["text"]
    assert {t["name"] for t in next(r for r in llm.requests if r["tag"] == "v2.orders")["tools"]} == {
        "get_order", "find_orders_by_email", "get_tracking", "submit_findings"}


def test_reviewer_gets_code_resolved_evidence():
    llm = FakeLLM(script_for())
    sb = Sandbox()
    run_v2(MSG, llm, sb, Meter())
    rev = next(r for r in llm.requests if r["tag"] == "v2.review")
    payload = json.loads(rev["messages"][0]["content"])
    assert payload["evidence"][0]["source_says"] == sb.log[0]["result"]["status"]
    assert rev["tools"][0]["name"] == "submit_review" and len(rev["tools"]) == 1


def test_worker_finding_with_bad_source_is_sent_back():
    bad = {"type": "tool", "call_id": "c1", "field": "nope"}
    llm = FakeLLM(script_for(worker_source=bad))
    out, trace = run_v2(MSG, llm, Sandbox(), Meter())
    worker = next(t for t in trace[0]["trace"] if t.get("worker") == "orders")
    submits = [t for t in worker["trace"] if t["type"] == "submit"]
    assert submits[0]["errors"] and not submits[1]["errors"]     # second submission (no findings) is accepted


def test_claims_not_from_findings_is_counted():
    kb_src = {"type": "kb", "doc_id": "faq", "lines": [1, 1]}
    out, _ = run_v2(MSG, FakeLLM(script_for(final_source=kb_src)), Sandbox(), Meter())
    assert out["meta"]["v2"]["claims_not_from_findings"] == 1


def test_orchestrator_that_never_submits_hands_off():
    out, _ = run_v2(MSG, FakeLLM(lambda req: {"content": [text("thinking")]}), Sandbox(), Meter())
    assert out["meta"]["handoff_reason"] == "no_action" and out["route"] == "ESCALATE"
