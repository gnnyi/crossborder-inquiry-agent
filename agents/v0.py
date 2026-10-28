"""V0: one agent, a hand-written loop, no framework.

    assemble context -> call model -> run the tools it asked for -> send results back -> repeat
    until it calls submit_response (or a limit is hit, and the message goes to a human).

`agent_loop` knows nothing about orders or the contract: it gets the tools, a function that runs a tool,
and a function that checks a submission. V2 reuses it for the orchestrator and every worker.
`run_v0` wires it to the sandbox and the output contract.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from agents import prompts
from agents.common import assemble_output, check_output, citation_errors, handoff_output, render_message, submit_tool, tool_result_payload
from agents.llm import Meter
from sandbox.tools import Sandbox, load_tool_schemas


@dataclass
class Limits:
    max_steps: int = 10            # model calls per message
    max_tool_calls: int = 12       # sandbox calls per message, retries included
    max_timeout_retries: int = 2   # extra attempts for one tool_use that timed out
    max_contract_fixes: int = 1    # times a rejected submission is sent back for correction
    max_nudges: int = 1            # times we prompt a model that neither called a tool nor submitted


@dataclass
class LoopResult:
    submitted: dict | None                 # the accepted submit input, or None
    handoff_reason: str | None             # why a human must take over (None when submitted)
    steps: int = 0
    tool_calls: int = 0
    trace: list[dict] = field(default_factory=list)


def agent_loop(llm, system: list[dict], user_text: str, tools: list[dict],
               run_tool: Callable[[str, dict], dict], check_submit: Callable[[dict], list[str]],
               limits: Limits, meter: Meter, tag: str, submit_name: str = "submit_response") -> LoopResult:
    messages: list[dict] = [{"role": "user", "content": user_text}]
    res = LoopResult(submitted=None, handoff_reason=None)
    tool_names = {t["name"] for t in tools}
    fixes = nudges = 0

    while res.steps < limits.max_steps:
        # ---- 1. call the model with the whole conversation so far
        res.steps += 1
        reply = llm.create(system=system, messages=messages, tools=tools, meter=meter, tag=tag)
        messages.append({"role": "assistant", "content": reply.content})   # thinking blocks go back unchanged
        uses = reply.tool_uses()
        res.trace.append({"step": res.steps, "type": "model", "stop_reason": reply.stop_reason,
                          "text": reply.text(), "tool_uses": [{"name": u["name"], "input": u["input"]} for u in uses]})

        # ---- 2. no tool and no submission: nudge once, then give up
        if not uses:
            if nudges >= limits.max_nudges:
                return _handoff(res, "no_action")
            nudges += 1
            messages.append({"role": "user", "content": prompts.NUDGE})
            res.trace.append({"step": res.steps, "type": "nudge"})
            continue

        # ---- 3. answer every tool_use in this turn, in one user message
        results = []
        for u in uses:
            if u["name"] == submit_name:
                errors = check_submit(u["input"])
                res.trace.append({"step": res.steps, "type": "submit", "errors": errors})
                if not errors:
                    res.submitted = u["input"]
                    return res
                if fixes >= limits.max_contract_fixes:
                    return _handoff(res, "contract_invalid")
                fixes += 1
                results.append(_result(u, "Submission rejected, fix these and submit again:\n- " + "\n- ".join(errors), error=True))
                continue

            if u["name"] not in tool_names:
                results.append(_result(u, f"Unknown tool {u['name']!r}.", error=True))
                continue

            # sandbox call; a timeout is retried a bounded number of times, every attempt is a logged call
            attempts = 0
            while True:
                if res.tool_calls >= limits.max_tool_calls:
                    return _handoff(res, "max_tool_calls")
                res.tool_calls += 1
                r = run_tool(u["name"], u["input"])
                res.trace.append({"step": res.steps, "type": "tool", "name": u["name"], "input": u["input"],
                                  "call_id": r.get("call_id"), "status": r["status"], "error": r.get("error")})
                if r["status"] != "timeout" or attempts >= limits.max_timeout_retries:
                    break
                attempts += 1
            # invalid_argument / not_found go back to the model as they are: it decides how to fix the call
            results.append(_result(u, tool_result_payload(r) if "call_id" in r else json.dumps(r, ensure_ascii=False),
                                   error=r["status"] != "ok"))

        messages.append({"role": "user", "content": results})

    return _handoff(res, "max_steps")


def _result(use: dict, content: str, error: bool = False) -> dict:
    block = {"type": "tool_result", "tool_use_id": use["id"], "content": content}
    if error:
        block["is_error"] = True
    return block


def _handoff(res: LoopResult, reason: str) -> LoopResult:
    res.handoff_reason = reason
    res.trace.append({"step": res.steps, "type": "handoff", "reason": reason})
    return res


# ======================================================================= V0 wiring
SANDBOX_TOOLS = load_tool_schemas()


def run_v0(msg: dict, llm, sandbox: Sandbox, meter: Meter, variant: str = "b", limits: Limits | None = None,
           run_id: str = "adhoc") -> tuple[dict, list[dict]]:
    """Returns (contract output, trace). variant "a": the model outputs route; "b": code derives it."""
    limits = limits or Limits()
    system = prompts.system_blocks(prompts.v0_instructions(variant))
    tools = SANDBOX_TOOLS + [submit_tool(variant)]

    def meta() -> dict:
        return {"arch": f"V0-{variant}", "model": llm.model, "tokens_in": meter.tokens_in, "tokens_out": meter.output_tokens,
                "cost_usd": round(meter.cost_usd, 6), "latency_ms": round(meter.api_ms), "run_id": run_id,
                "cache_read_tokens": meter.cache_read_tokens, "cache_write_tokens": meter.cache_write_tokens,
                "llm_calls": meter.calls}

    def check(submitted: dict) -> list[str]:
        # same checks the scorer runs, against what the sandbox actually logged
        errors = check_output(assemble_output(msg["msg_id"], submitted, sandbox.log, meta(), variant), sandbox.log)
        return errors or citation_errors(submitted, sandbox.log)

    res = agent_loop(llm, system, render_message(msg), tools, sandbox.call, check, limits, meter, tag="v0")
    if res.submitted is None:
        out = handoff_output(msg["msg_id"], res.handoff_reason, sandbox.log, meta())
    else:
        out = assemble_output(msg["msg_id"], res.submitted, sandbox.log, meta(), variant)
    out["meta"].update(steps=res.steps, tool_calls_made=res.tool_calls)
    return out, res.trace
