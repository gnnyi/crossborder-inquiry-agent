"""Scripted stand-in for the model, used when there is no API key and in tests.

It is NOT an agent and its outputs are NOT results: it exists only to drive the real loop, the sandbox,
the contract checks and the run directory end to end. Every output it produces is labelled model="fake".

The script reacts to the tools offered in the request (V0 / V1 / V2 offer different ones), not to
message content, apart from pulling a digits-only order number out of the first user turn.
"""
from __future__ import annotations

import json
import re

from agents.llm import Reply, text, tool_use

ORDER_RE = re.compile(r"\b(1\d{4})\b")
USAGE = {"input_tokens": 120, "output_tokens": 60, "cache_write_tokens": 0, "cache_read_tokens": 0}


def _reply(blocks: list[dict]) -> Reply:
    stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return Reply(content=blocks, stop_reason=stop, usage=dict(USAGE))


def _first_user_text(req: dict) -> str:
    c = req["messages"][0]["content"]
    return c if isinstance(c, str) else json.dumps(c)


def _ok_results(req: dict) -> list[dict]:
    """Tool results the model has seen so far that came back ok, as parsed payloads."""
    out = []
    for m in req["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result" and not b.get("is_error"):
                    try:
                        p = json.loads(b["content"])
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(p, dict) and p.get("status") == "ok":
                        out.append(p)
    return out


def _calls_made(req: dict) -> int:
    return sum(1 for m in req["messages"] if m["role"] == "assistant"
               for b in m["content"] if b["type"] == "tool_use" and not b["name"].startswith("submit"))


def _reply_body(results: list[dict]) -> dict:
    claims = [{"text": "Returns can be started within 30 days of delivery.",
               "source": {"type": "kb", "doc_id": "policy_returns", "lines": [5, 5]}}]
    for p in results:
        if "call_id" in p and isinstance(p.get("result"), dict) and "status" in p["result"]:
            claims.append({"text": f"Your order status is {p['result']['status']}.",
                           "source": {"type": "tool", "call_id": p["call_id"], "field": "status"}})
            break
    return {"text": "Hi, thanks for reaching out. " + " ".join(c["text"] for c in claims) + " Northwren Care", "claims": claims}


def smoke_script(req: dict) -> Reply:
    names = {t["name"] for t in req["tools"]}
    user = _first_user_text(req)
    order = ORDER_RE.search(user)

    if "classify" in names:                                    # V1 classify node
        return _reply([tool_use("classify", {"intents": ["OTHER"], "priority": "P2", "risk_flags": [],
                                             "order_refs": [order.group(1)] if order else [], "skus": [],
                                             "needs": ["order"] if order else [], "lookup_sender_email": not order})])
    if "submit_draft" in names:                                # V1 draft node
        return _reply([tool_use("submit_draft", {"reply": _reply_body(_ok_results(req)), "handoff_note": None})])
    if "submit_findings" in names:                             # V2 worker
        if "get_order" in names and order and _calls_made(req) == 0:
            return _reply([tool_use("get_order", {"order_id": order.group(1)})])
        findings = [{"statement": f"order status is {p['result']['status']}",
                     "source": {"type": "tool", "call_id": p["call_id"], "field": "status"}}
                    for p in _ok_results(req) if isinstance(p.get("result"), dict) and "status" in p["result"]][:1]
        return _reply([tool_use("submit_findings", {"findings": findings, "unresolved": [], "notes": "smoke"})])
    if "submit_review" in names:                               # V2 reviewer
        return _reply([tool_use("submit_review", {"verdict": "pass", "issues": []})])
    if "delegate" in names:                                    # V2 orchestrator
        made = _calls_made(req)
        if made == 0 and order:
            return _reply([tool_use("delegate", {"worker": "orders", "brief": {"task": "Look up the order", "context": f"order {order.group(1)}", "questions": ["status"]}})])
        sub = {"intents": ["OTHER"], "priority": "P2", "risk_flags": [], "reply": _reply_body(_worker_results(req)), "handoff_note": None}
        return _reply([tool_use("submit_response", sub)])

    # V0: one lookup if an order number is visible, then submit
    if order and _calls_made(req) == 0 and "get_order" in names:
        return _reply([text("Looking up the order."), tool_use("get_order", {"order_id": order.group(1)})])
    sub = {"intents": ["OTHER"], "priority": "P2", "risk_flags": [], "reply": _reply_body(_ok_results(req)), "handoff_note": None}
    if "route" in next(t for t in req["tools"] if t["name"] == "submit_response")["input_schema"]["properties"]:
        sub["route"] = "AUTO_REPLY"
    return _reply([tool_use("submit_response", sub)])


def _worker_results(req: dict) -> list[dict]:
    """V2: findings returned by workers, shaped like tool payloads so _reply_body can cite them."""
    out = []
    for p in _ok_results(req):
        for f in (p.get("result") or {}).get("findings", []):
            src = f.get("source", {})
            if src.get("type") == "tool":
                out.append({"call_id": src["call_id"], "result": {"status": "x"}})
    return out
