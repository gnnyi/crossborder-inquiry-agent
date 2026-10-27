"""V2: orchestrator-worker. The orchestrator (a model) decides which workers to brief; each worker is its own
small agent loop (agents/v0.py::agent_loop) with its own context, its own tools and nothing else.

What crosses the boundary is explicit and is the whole point of this architecture:

    orchestrator -> worker   Brief    {"task", "context", "questions"}   (+ "draft" for the reviewer)
    worker -> orchestrator   Findings {"findings": [{"statement", "source"}], "unresolved": [...], "notes"}
    reviewer -> orchestrator Review   {"verdict": "pass" | "revise", "issues": [{"type", "detail"}]}

Workers never see the conversation; the orchestrator never sees the knowledge base or raw tool results.
All workers share one Sandbox, so call ids are unique per message and claims can cite them.
"""
from __future__ import annotations

import json
from collections import Counter

from agents import prompts
from agents.common import assemble_output, check_output, handoff_output, render_message, submit_tool
from agents.llm import Meter
from agents.v0 import SANDBOX_TOOLS, Limits, agent_loop
from sandbox import kb as kbmod
from sandbox.tools import Sandbox
from scoring.score import MISSING, resolve_field

ORCH_LIMITS = Limits(max_steps=8, max_tool_calls=6)          # "tool calls" here are delegations
WORKER_LIMITS = Limits(max_steps=6, max_tool_calls=6)
REVIEW_LIMITS = Limits(max_steps=2, max_tool_calls=0)

SOURCE_SCHEMA = {"type": "object", "description": '{"type":"tool","call_id","field"} or {"type":"kb","doc_id","lines":[start,end]}'}
BRIEF_SCHEMA = {"type": "object", "required": ["task", "context", "questions"], "additionalProperties": False,
                "properties": {"task": {"type": "string"}, "context": {"type": "string"},
                               "questions": {"type": "array", "items": {"type": "string"}}}}

DELEGATE_TOOL = {
    "name": "delegate",
    "description": "Send a brief to a worker and get its findings back. Workers: orders, policy, review.",
    "input_schema": {"type": "object", "required": ["worker", "brief"], "additionalProperties": False, "properties": {
        "worker": {"enum": ["orders", "policy", "review"]},
        "brief": BRIEF_SCHEMA,
        "draft": {"type": "object", "description": "review only: the reply you plan to submit, {text, claims}"}}},
}
FINDINGS_TOOL = {
    "name": "submit_findings",
    "description": "Return your findings to the orchestrator. Call exactly once, at the end.",
    "input_schema": {"type": "object", "required": ["findings", "unresolved", "notes"], "additionalProperties": False, "properties": {
        "findings": {"type": "array", "items": {"type": "object", "required": ["statement", "source"],
                                                "properties": {"statement": {"type": "string"}, "source": SOURCE_SCHEMA}}},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"}}},
}
REVIEW_TOOL = {
    "name": "submit_review",
    "description": "Return the review verdict. Call exactly once.",
    "input_schema": {"type": "object", "required": ["verdict", "issues"], "additionalProperties": False, "properties": {
        "verdict": {"enum": ["pass", "revise"]},
        "issues": {"type": "array", "items": {"type": "object", "required": ["type", "detail"], "properties": {
            "type": {"enum": ["unsourced_fact", "unauthorised_commitment", "contradiction"]}, "detail": {"type": "string"}}}}}},
}
WORKER_TOOLS = {
    "orders": [t for t in SANDBOX_TOOLS if t["name"] in ("get_order", "find_orders_by_email", "get_tracking")],
    "policy": [t for t in SANDBOX_TOOLS if t["name"] == "get_product"],
}


def source_errors(src: dict, sandbox: Sandbox) -> str | None:
    """Why a source cannot be cited, or None. Same rules as the scorer's ghost-citation check."""
    if not isinstance(src, dict):
        return "source must be an object"
    if src.get("type") == "kb":
        lines = src.get("lines")
        if not (isinstance(lines, list) and len(lines) == 2 and kbmod.is_valid_span(src.get("doc_id"), *lines)):
            return f"no such kb span {src.get('doc_id')}:{lines}"
        return None
    if src.get("type") == "tool":
        rec = next((r for r in sandbox.log if r["call_id"] == src.get("call_id")), None)
        if rec is None or rec["status"] != "ok":
            return f"call {src.get('call_id')} does not exist or did not return ok"
        if resolve_field(rec["result"], src.get("field", "")) is MISSING:
            return f"field {src.get('field')!r} not in {src.get('call_id')}"
        return None
    return "source type must be kb or tool"


def evidence_for(claims: list, sandbox: Sandbox) -> list[dict]:
    """What each claim's source actually says, resolved by code, for the reviewer."""
    out = []
    for c in claims if isinstance(claims, list) else []:
        src = c.get("source", {}) if isinstance(c, dict) else {}
        err = source_errors(src, sandbox)
        if err:
            value = f"(invalid source: {err})"
        elif src["type"] == "kb":
            value = "\n".join(kbmod.get_lines(src["doc_id"], *src["lines"]))
        else:
            rec = next(r for r in sandbox.log if r["call_id"] == src["call_id"])
            value = resolve_field(rec["result"], src["field"])
        out.append({"claim": c.get("text") if isinstance(c, dict) else c, "source": src, "source_says": value})
    return out


def _interleave(orch: list[dict], workers: list[dict]) -> list[dict]:
    """Put each worker's trace right after the delegate call that started it (workers run in call order)."""
    pending, merged = list(workers), []
    for e in orch:
        if e["type"] == "tool" and e["name"] == "delegate" and e["status"] != "invalid_argument" and pending:
            merged.append(pending.pop(0))
        merged.append(e)
    return merged + pending


def run_v2(msg: dict, llm, sandbox: Sandbox, meter: Meter, run_id: str = "adhoc") -> tuple[dict, list[dict]]:
    trace: list[dict] = []
    stats = {"delegations": Counter(), "review_verdicts": [], "worker_handoffs": 0, "unresolved": 0}
    cited_by_workers: set[str] = set()

    def run_worker(kind: str, brief: dict, draft: dict | None) -> dict:
        if kind == "review":
            system = prompts.system_blocks(prompts.v2_reviewer_instructions(), with_kb=False)
            user = json.dumps({"brief": brief, "draft_text": (draft or {}).get("text"),
                               "evidence": evidence_for((draft or {}).get("claims", []), sandbox)}, ensure_ascii=False, default=str)
            tools, limits, check = [REVIEW_TOOL], REVIEW_LIMITS, lambda s: []
            submit = "submit_review"
        else:
            system = prompts.system_blocks(prompts.v2_worker_instructions(kind), with_kb=(kind == "policy"))
            user = "<brief>\n" + json.dumps(brief, ensure_ascii=False) + "\n</brief>"
            tools, limits, submit = WORKER_TOOLS[kind] + [FINDINGS_TOOL], WORKER_LIMITS, "submit_findings"

            def check(s: dict) -> list[str]:
                return [f"findings[{i}]: {e}" for i, f in enumerate(s.get("findings", []))
                        if (e := source_errors(f.get("source") if isinstance(f, dict) else None, sandbox))]

        res = agent_loop(llm, system, user, tools, sandbox.call, check, limits, meter, tag=f"v2.{kind}", submit_name=submit)
        trace.append({"type": "worker", "worker": kind, "brief": brief, "steps": res.steps, "handoff": res.handoff_reason,
                      "trace": res.trace, "returned": res.submitted})
        if res.submitted is None:
            stats["worker_handoffs"] += 1
            return {"status": "failed", "error": f"{kind} worker could not finish ({res.handoff_reason})"}
        if kind == "review":
            stats["review_verdicts"].append(res.submitted["verdict"])
        else:
            stats["unresolved"] += len(res.submitted.get("unresolved", []))
            cited_by_workers.update(json.dumps(f["source"], sort_keys=True) for f in res.submitted["findings"])
        return {"status": "ok", "result": res.submitted}

    def delegate(name: str, args: dict) -> dict:
        kind = args.get("worker")
        if kind not in ("orders", "policy", "review") or not isinstance(args.get("brief"), dict):
            return {"status": "invalid_argument", "error": "worker must be orders|policy|review and brief an object"}
        stats["delegations"][kind] += 1
        return run_worker(kind, args["brief"], args.get("draft"))

    def meta() -> dict:
        return {"arch": "V2", "model": llm.model, "tokens_in": meter.tokens_in, "tokens_out": meter.output_tokens,
                "cost_usd": round(meter.cost_usd, 6), "latency_ms": round(meter.api_ms), "run_id": run_id,
                "cache_read_tokens": meter.cache_read_tokens, "cache_write_tokens": meter.cache_write_tokens,
                "llm_calls": meter.calls}

    def check(submitted: dict) -> list[str]:
        return check_output(assemble_output(msg["msg_id"], submitted, sandbox.log, meta(), "b"), sandbox.log)

    system = prompts.system_blocks(prompts.v2_orchestrator_instructions(), with_kb=False)
    res = agent_loop(llm, system, render_message(msg), [DELEGATE_TOOL, submit_tool("b")], delegate, check,
                     ORCH_LIMITS, meter, tag="v2.orchestrator")
    trace = [{"type": "orchestrator", "trace": _interleave(res.trace, trace)}]
    if res.submitted is None:
        out = handoff_output(msg["msg_id"], res.handoff_reason, sandbox.log, meta())
    else:
        out = assemble_output(msg["msg_id"], res.submitted, sandbox.log, meta(), "b")
    claims = (out["reply"] or {}).get("claims", [])
    out["meta"].update(steps=res.steps, tool_calls_made=len(sandbox.log), v2={
        "delegations": dict(stats["delegations"]),
        "reviewed": bool(stats["review_verdicts"]),
        "review_verdicts": stats["review_verdicts"],
        "worker_handoffs": stats["worker_handoffs"],
        "unresolved_questions": stats["unresolved"],
        # claims whose source no worker returned: the orchestrator made the source up or reworded it
        "claims_not_from_findings": sum(1 for c in claims if json.dumps(c["source"], sort_keys=True) not in cited_by_workers),
        "claims_total": len(claims),
    })
    return out, trace
