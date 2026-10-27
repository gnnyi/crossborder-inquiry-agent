"""V1: a fixed workflow. Code owns the control flow; the model fills in two judgement steps.

    classify (model) -> fetch (code) -> draft (model) -> risk_gate (code)

The edges never change. The model never chooses a tool: `fetch` decides from the classification which
lookups to run, and it is also where order numbers are cleaned up ("#10231", "NW-10231", "1O231" -> "10231").
That is allowed here and not in V0: in a workflow, turning text into lookup arguments is the code's job;
in an agent it is the model's. The sandbox itself still repairs nothing.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agents import prompts
from agents.common import assemble_output, check_output, handoff_output, render_message, tool_result_payload
from agents.llm import Meter
from scoring.policy import intent_codes, risk_flags
from scoring.route import derive_route
from sandbox.tools import Sandbox

MAX_ORDERS = 3
MAX_TIMEOUT_RETRIES = 2
NEEDS = ["order", "tracking", "product", "orders_by_email"]

CLASSIFY_TOOL = {
    "name": "classify",
    "description": "Report the triage and the data the reply needs. Call exactly once.",
    "input_schema": {
        "type": "object", "additionalProperties": False,
        "required": ["intents", "priority", "risk_flags", "order_refs", "skus", "needs"],
        "properties": {
            "intents": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"enum": intent_codes()}},
            "priority": {"enum": ["P0", "P1", "P2"]},
            "risk_flags": {"type": "array", "items": {"enum": risk_flags()}},
            "order_refs": {"type": "array", "items": {"type": "string"}, "description": "Order numbers exactly as the customer wrote them."},
            "skus": {"type": "array", "items": {"type": "string"}, "description": "SKUs (AAA-000) of products asked about."},
            "needs": {"type": "array", "items": {"enum": NEEDS}},
        },
    },
}

DRAFT_TOOL = {
    "name": "submit_draft",
    "description": "Submit the reply draft. Call exactly once.",
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["reply", "handoff_note"],
        "properties": {
            "reply": {"type": "object", "required": ["text", "claims"], "properties": {
                "text": {"type": "string"},
                "claims": {"type": "array", "items": {"type": "object", "required": ["text", "source"],
                                                       "properties": {"text": {"type": "string"}, "source": {"type": "object"}}}}}},
            "handoff_note": {"type": ["string", "null"]},
        },
    },
}


class State(TypedDict, total=False):
    msg: dict
    classification: dict | None
    fetched: list[dict]
    draft: dict | None
    output: dict
    handoff_reason: str | None
    trace: list[dict]


# ------------------------------------------------------------------ code helpers
ORDER_DIGITS = re.compile(r"\d{4,8}")


def normalize_order_ref(ref: Any) -> str | None:
    """'#10231', 'NW-10231', 'nw10231', '1O231', 10231 -> '10231'. Letter O between digits is read as zero."""
    s = str(ref).strip()
    s = re.sub(r"(?<=\d)[oO]|[oO](?=\d)", "0", s)
    m = ORDER_DIGITS.search(s)
    return m.group(0) if m else None


def _ask_tool(llm, system, user_text: str, tool: dict, meter: Meter, tag: str, trace: list, check) -> dict | None:
    """One model call that must end in `tool`; one retry with the validation errors. None if both fail."""
    messages = [{"role": "user", "content": user_text}]
    for attempt in range(2):
        reply = llm.create(system=system, messages=messages, tools=[tool], meter=meter, tag=tag)
        uses = [u for u in reply.tool_uses() if u["name"] == tool["name"]]
        errors = check(uses[0]["input"]) if uses else [f"call the {tool['name']} tool"]
        trace.append({"type": "model", "node": tag, "tool_uses": [{"name": u["name"], "input": u["input"]} for u in uses],
                      "text": reply.text(), "errors": errors})
        if not errors:
            return uses[0]["input"]
        messages.append({"role": "assistant", "content": reply.content})
        if uses:
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": uses[0]["id"], "is_error": True,
                                                          "content": "Rejected, fix and call again:\n- " + "\n- ".join(errors)}]
                             + [{"type": "tool_result", "tool_use_id": u["id"], "content": "ignored"} for u in reply.tool_uses()[1:]]})
        else:
            messages.append({"role": "user", "content": f"Call the {tool['name']} tool now."})
    return None


def _check_classification(c: dict) -> list[str]:
    errs = []
    if not (1 <= len(c.get("intents", [])) <= 2) or any(i not in intent_codes() for i in c.get("intents", [])):
        errs.append("intents: 1-2 codes from the intent list")
    if c.get("priority") not in ("P0", "P1", "P2"):
        errs.append("priority: P0, P1 or P2")
    if any(f not in risk_flags() for f in c.get("risk_flags", [])):
        errs.append("risk_flags: only flags from the list")
    if any(n not in NEEDS for n in c.get("needs", [])):
        errs.append(f"needs: only {NEEDS}")
    return errs


def _combine(c: dict, d: dict) -> dict:
    """Classification + draft -> submission. A routed-to-human message always gets a handoff note."""
    sub = {**{k: c[k] for k in ("intents", "priority", "risk_flags")}, **d}
    if derive_route(c["risk_flags"]) != "AUTO_REPLY" and not sub.get("handoff_note"):
        sub["handoff_note"] = "Risk flags: " + ", ".join(c["risk_flags"])
    return sub


# ------------------------------------------------------------------ graph
def build_graph(llm, sandbox: Sandbox, meter: Meter, run_id: str):
    def meta(trace: list) -> dict:
        return {"arch": "V1", "model": llm.model, "tokens_in": meter.tokens_in, "tokens_out": meter.output_tokens,
                "cost_usd": round(meter.cost_usd, 6), "latency_ms": round(meter.api_ms), "run_id": run_id,
                "cache_read_tokens": meter.cache_read_tokens, "cache_write_tokens": meter.cache_write_tokens,
                "llm_calls": meter.calls, "steps": meter.calls}

    def classify(s: State) -> State:
        system = prompts.system_blocks(prompts.v1_classify_instructions())
        c = _ask_tool(llm, system, render_message(s["msg"]), CLASSIFY_TOOL, meter, "v1.classify", s["trace"], _check_classification)
        return {"classification": c, "handoff_reason": None if c else "classify_failed"}

    def call(name: str, args: dict, trace: list) -> dict:
        for attempt in range(MAX_TIMEOUT_RETRIES + 1):
            r = sandbox.call(name, args)
            trace.append({"type": "tool", "node": "v1.fetch", "name": name, "input": args, "call_id": r["call_id"], "status": r["status"], "error": r["error"]})
            if r["status"] != "timeout":
                break
        return r

    def fetch(s: State) -> State:
        c, trace, got = s["classification"], s["trace"], []
        if not c:
            return {"fetched": []}
        needs = set(c["needs"])
        order_ids = list(dict.fromkeys(filter(None, (normalize_order_ref(r) for r in c["order_refs"]))))[:MAX_ORDERS]
        if not order_ids and ({"orders_by_email", "order", "tracking"} & needs) and "@" in s["msg"]["from"]:
            r = call("find_orders_by_email", {"email": s["msg"]["from"]}, trace)
            got.append(r)
            if r["status"] == "ok" and len(r["result"]["orders"]) == 1:      # only when unambiguous
                order_ids = [r["result"]["orders"][0]["order_id"]]
        for oid in order_ids:
            if {"order", "tracking"} & needs or not needs:
                got.append(call("get_order", {"order_id": oid}, trace))
            if "tracking" in needs:
                got.append(call("get_tracking", {"order_id": oid}, trace))
        for sku in dict.fromkeys(x.strip().upper() for x in c["skus"]):
            got.append(call("get_product", {"sku": sku}, trace))
        return {"fetched": got}

    def draft(s: State) -> State:
        c = s["classification"]
        if not c:
            return {"draft": None}
        system = prompts.system_blocks(prompts.v1_draft_instructions())
        data = "\n".join(f"{r['tool']}: {tool_result_payload(r)}" for r in s["fetched"]) or "(no lookups were made)"
        user = (render_message(s["msg"]) + "\n\n<classification>\n" + json.dumps(c, ensure_ascii=False)
                + f"\nRoute that code will apply: {derive_route(c['risk_flags'])}\n</classification>\n\n<lookups>\n" + data + "\n</lookups>")

        def check(d: dict) -> list[str]:
            sub = _combine(c, d)
            return check_output(assemble_output(s["msg"]["msg_id"], sub, sandbox.log, meta(s["trace"]), "b"), sandbox.log)

        d = _ask_tool(llm, system, user, DRAFT_TOOL, meter, "v1.draft", s["trace"], check)
        return {"draft": d, "handoff_reason": None if d else "draft_contract_invalid"}

    def risk_gate(s: State) -> State:
        mid = s["msg"]["msg_id"]
        if s.get("handoff_reason") or not s.get("draft"):
            out = handoff_output(mid, s.get("handoff_reason") or "no_draft", sandbox.log, meta(s["trace"]))
        else:
            out = assemble_output(mid, _combine(s["classification"], s["draft"]), sandbox.log, meta(s["trace"]), variant="b")
        s["trace"].append({"type": "gate", "route": out["route"], "handoff_reason": s.get("handoff_reason")})
        return {"output": out}

    g = StateGraph(State)
    for name, fn in [("classify", classify), ("fetch", fetch), ("draft", draft), ("risk_gate", risk_gate)]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_edge("classify", "fetch")
    g.add_edge("fetch", "draft")
    g.add_edge("draft", "risk_gate")
    g.add_edge("risk_gate", END)
    return g.compile()


def run_v1(msg: dict, llm, sandbox: Sandbox, meter: Meter, run_id: str = "adhoc") -> tuple[dict, list[dict]]:
    trace: list[dict] = []
    final = build_graph(llm, sandbox, meter, run_id).invoke({"msg": msg, "trace": trace, "fetched": []})
    out = final["output"]
    out["meta"].update(tool_calls_made=len(sandbox.log))
    return out, trace
