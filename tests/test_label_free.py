"""scoring/label_free.py: every metric runs with no gold labels at all."""
from __future__ import annotations

import json

from scoring.label_free import failure_index, run_metrics, unsourced_facts
from scoring.score import load_run
from sandbox.tools import Sandbox


def _out(mid, route="AUTO_REPLY", flags=(), reply=None, calls=(), meta_extra=None):
    meta = {"arch": "T", "model": "m", "tokens_in": 1000, "tokens_out": 10, "cost_usd": 0.01, "latency_ms": 100,
            "run_id": "r", "cache_read_tokens": 800, "steps": 2, "llm_calls": 2, **(meta_extra or {})}
    return {"msg_id": mid, "intents": ["WISMO"], "priority": "P1", "risk_flags": list(flags), "route": route,
            "tool_calls": list(calls), "reply": reply, "handoff_note": "h" if route != "AUTO_REPLY" else None, "meta": meta}


def _build(tmp_path):
    run = tmp_path / "T" / "r"
    for d in ("outputs", "tool_logs", "errors"):
        (run / d).mkdir(parents=True)
    (run / "run_meta.json").write_text(json.dumps({"arch": "T", "run_id": "r", "model": "m"}))
    sb = Sandbox(log_path=run / "tool_logs" / "m001.jsonl")
    sb.get_order("#10231")
    r = sb.get_order("10231")
    calls = [{"call_id": x["call_id"], "name": x["tool"], "args": x["args"], "status": x["status"]} for x in sb.log]
    order = r["result"]
    claims = [{"text": "Status.", "source": {"type": "tool", "call_id": "c2", "field": "status"}},
              {"text": "Ghost.", "source": {"type": "tool", "call_id": "c2", "field": "no_such_field"}}]
    reply = {"text": f"Your order 10231 total was ${order['total']:.2f}.", "claims": claims}
    outs = {
        "m001": _out("m001", reply=reply, calls=calls),
        "m002": _out("m002", route="AUTO_REPLY", flags=["REFUND_REQUEST"], reply={"text": "Hi", "claims": []}),  # self-contradiction
        "m003": _out("m003", route="ESCALATE", meta_extra={"fallback": True, "handoff_reason": "max_steps"}),
    }
    for mid, o in outs.items():
        (run / "outputs" / f"{mid}.json").write_text(json.dumps(o))
    (run / "errors" / "m004.json").write_text(json.dumps({"msg_id": "m004", "error_type": "exception", "message": "x"}))
    return run


def test_metrics_without_labels(tmp_path):
    m, rows = run_metrics(load_run(_build(tmp_path)))
    assert m["n"] == 4
    assert m["contract_ok_rate"] == 0.75 and m["agent_completed_rate"] == 0.5 and m["failure_rate"] == 0.5
    assert m["_fallbacks"] == {"max_steps": 1} and m["_failures"] == {"exception": 1}
    assert m["ghost_claims"] == 1
    assert m["invalid_arg_calls"] == 1
    assert m["unsourced_mentions"] == 2          # order number and amount are not in any claim
    assert m["route_self_contradiction_rate"] == 0.5
    assert m["route_share"]["FAILED"] == 0.25
    idx = failure_index(rows)
    assert idx["ghost_citation"] == ["m001"] and idx["handoff:max_steps"] == ["m003"]
    assert idx["route_self_contradiction"] == ["m002"] and idx["failure:exception"] == ["m004"]


def test_unsourced_facts_coverage():
    claims = [{"text": "It shipped on 26 September 2026 and cost $59.00 (order 10231).", "source": {}}]
    assert unsourced_facts({"text": "Order 10231 shipped Sept 26 and cost $59.00.", "claims": claims}) == []
    missing = unsourced_facts({"text": "It arrives on 3 October and the refund is $12.50.", "claims": claims})
    assert len(missing) == 2
    assert unsourced_facts(None) == []
