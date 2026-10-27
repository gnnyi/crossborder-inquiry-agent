"""V0 loop behaviour: every stop condition and failure path in agents/v0.py, driven by FakeLLM."""
from __future__ import annotations

import json

import pytest

import run as run_mod
from agents.common import RunDir, check_output, get_message, submit_tool
from agents.llm import FakeLLM, Meter, text, tool_use
from agents.v0 import Limits, agent_loop, run_v0
from sandbox.tools import Sandbox

MSG = get_message("m003")          # mentions order 10231


def _submit(**over):
    sub = {"intents": ["WISMO"], "priority": "P1", "risk_flags": [], "handoff_note": None,
           "reply": {"text": "Hi, thanks. Northwren Care", "claims": []}}
    sub.update(over)
    return {"content": [tool_use("submit_response", sub)]}


def _run(script, variant="b", limits=None):
    sb = Sandbox()
    out, trace = run_v0(MSG, FakeLLM(script), sb, Meter(), variant=variant, limits=limits)
    return out, trace, sb


def test_lookup_then_submit_builds_valid_output_with_logged_calls():
    claim = {"text": "Your order status is shipped.", "source": {"type": "tool", "call_id": "c1", "field": "status"}}
    out, trace, sb = _run([{"content": [tool_use("get_order", {"order_id": "10231"})]},
                           _submit(reply={"text": "Hi. Northwren Care", "claims": [claim]})])
    assert check_output(out, sb.log) == []
    assert out["tool_calls"] == [{"call_id": "c1", "name": "get_order", "args": {"order_id": "10231"}, "status": "ok"}]
    assert out["route"] == "AUTO_REPLY" and out["meta"]["steps"] == 2


def test_variant_b_derives_route_from_flags_and_hides_route_from_model():
    assert "route" not in submit_tool("b")["input_schema"]["properties"]
    out, _, _ = _run([_submit(risk_flags=["REFUND_REQUEST"], handoff_note="refund")])
    assert out["route"] == "DRAFT_FOR_REVIEW"
    out, _, _ = _run([_submit(risk_flags=["REFUND_REQUEST", "CHARGEBACK_LEGAL"], reply=None, handoff_note="dispute")])
    assert out["route"] == "ESCALATE"


def test_variant_a_uses_model_route():
    assert "route" in submit_tool("a")["input_schema"]["properties"]
    out, _, _ = _run([_submit(route="AUTO_REPLY", risk_flags=["REFUND_REQUEST"])], variant="a")
    assert out["route"] == "AUTO_REPLY"       # the model's call stands, even when it contradicts policy


def test_invalid_argument_goes_back_to_model_unrepaired():
    llm = FakeLLM([{"content": [tool_use("get_order", {"order_id": "#10231"})]},
                   {"content": [tool_use("get_order", {"order_id": "10231"})]}, _submit()])
    sb = Sandbox()
    out, _ = run_v0(MSG, llm, sb, Meter())
    assert [r["status"] for r in sb.log] == ["invalid_argument", "ok"]
    result = llm.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "digits only" in json.loads(result["content"])["error"]
    assert check_output(out, sb.log) == []


def _loop(script, run_tool, limits=Limits(), check=lambda s: []):
    tools = [{"name": "get_order", "input_schema": {"type": "object"}}]
    return agent_loop(FakeLLM(script), [], "hi", tools, run_tool, check, limits, Meter(), tag="t")


def test_timeout_is_retried_then_succeeds():
    statuses = iter(["timeout", "timeout", "ok"])
    calls = []

    def run_tool(name, args):
        calls.append(args)
        return {"call_id": f"c{len(calls)}", "status": next(statuses), "result": {} , "error": None}

    llm = FakeLLM([{"content": [tool_use("get_order", {"order_id": "1"})]}, _submit()])
    res = agent_loop(llm, [], "hi", [{"name": "get_order"}], run_tool, lambda s: [], Limits(max_timeout_retries=2), Meter(), "t")
    assert len(calls) == 3 and res.submitted is not None
    sent = llm.requests[1]["messages"][-1]["content"]
    assert len(sent) == 1 and json.loads(sent[0]["content"])["status"] == "ok"


def test_timeout_retries_are_bounded():
    calls = []

    def run_tool(name, args):
        calls.append(args)
        return {"call_id": f"c{len(calls)}", "status": "timeout", "result": None, "error": "upstream timeout"}

    res = _loop([{"content": [tool_use("get_order", {"order_id": "1"})]}, _submit()], run_tool, Limits(max_timeout_retries=1))
    assert len(calls) == 2 and res.submitted is not None


def test_bad_submission_gets_one_fix_then_handoff():
    out, trace, sb = _run([_submit(intents=["NOT_AN_INTENT"]), _submit(intents=["ALSO_BAD"])])
    assert out["route"] == "ESCALATE" and out["meta"]["handoff_reason"] == "contract_invalid"
    assert check_output(out, sb.log) == []
    assert [t["type"] for t in trace].count("submit") == 2


def test_bad_submission_fixed_on_second_try():
    out, _, _ = _run([_submit(intents=["NOT_AN_INTENT"]), _submit()])
    assert out["intents"] == ["WISMO"] and not out["meta"].get("fallback")


def test_claim_citing_a_call_that_was_not_made_is_rejected():
    bad = {"text": "x", "source": {"type": "tool", "call_id": "c9", "field": "status"}}
    out, trace, _ = _run([_submit(reply={"text": "Hi", "claims": [bad]}), _submit()])
    first = next(t for t in trace if t["type"] == "submit")
    assert any("c9" in e for e in first["errors"]) and not out["meta"].get("fallback")


def test_no_tool_no_submit_nudges_once_then_hands_off():
    out, trace, _ = _run([{"content": [text("Let me think.")]}, {"content": [text("Still thinking.")]}])
    assert out["meta"]["handoff_reason"] == "no_action" and [t["type"] for t in trace].count("nudge") == 1


def test_nudge_can_recover():
    out, _, _ = _run([{"content": [text("hmm")]}, _submit()])
    assert not out["meta"].get("fallback")


def test_max_steps_hands_off():
    looping = lambda req: {"content": [tool_use("get_order", {"order_id": "10231"})]}
    out, _, sb = _run(looping, limits=Limits(max_steps=3, max_tool_calls=99))
    assert out["meta"]["handoff_reason"] == "max_steps" and len(sb.log) == 3
    assert check_output(out, sb.log) == []


def test_max_tool_calls_hands_off():
    looping = lambda req: {"content": [tool_use("get_order", {"order_id": "10231"})]}
    out, _, sb = _run(looping, limits=Limits(max_steps=99, max_tool_calls=4))
    assert out["meta"]["handoff_reason"] == "max_tool_calls" and len(sb.log) == 4


def test_unknown_tool_is_not_sent_to_sandbox():
    out, _, sb = _run([{"content": [tool_use("delete_order", {"order_id": "1"})]}, _submit()])
    assert sb.log == [] and not out["meta"].get("fallback")


# ------------------------------------------------------------------ run.py / run directory
def test_runner_crash_and_bad_output_go_to_errors_not_outputs(tmp_path):
    rd = RunDir.open("V0-b", "t1", {"arch": "V0-b", "model": "fake"}, root=tmp_path)

    def crash(msg, llm, sb, meter, run_id):
        raise RuntimeError("kaput")

    def bad(msg, llm, sb, meter, run_id):
        return {"msg_id": msg["msg_id"], "intents": []}, []

    assert run_mod.process(MSG, rd, crash, None, None, "t1") == "error"
    assert run_mod.process(get_message("m001"), rd, bad, None, None, "t1") == "error"
    assert not list((rd.path / "outputs").glob("*.json"))
    errs = {p.stem: json.loads(p.read_text()) for p in (rd.path / "errors").glob("*.json")}
    assert errs["m003"]["error_type"] == "exception" and "kaput" in errs["m003"]["message"]
    assert errs["m001"]["message"].startswith("contract:")


def test_resume_skips_done_and_moves_partial_logs_aside(tmp_path):
    argv = ["--arch", "V0", "--run-id", "r", "--fake", "--limit", "3", "--concurrency", "1", "--runs-dir", str(tmp_path)]
    assert run_mod.main(argv) == 0
    rd = tmp_path / "V0-b" / "r"
    (rd / "outputs" / "m003.json").unlink()                     # simulate a kill mid-message: log left, no output
    assert run_mod.main(argv) == 0
    assert len(list((rd / "outputs").glob("*.json"))) == 3
    assert list((rd / "traces" / "aborted").glob("m003.*.jsonl"))


def test_run_dir_refuses_a_different_config(tmp_path):
    RunDir.open("V0-b", "r", {"arch": "V0-b", "model": "a"}, root=tmp_path)
    with pytest.raises(SystemExit):
        RunDir.open("V0-b", "r", {"arch": "V0-b", "model": "b"}, root=tmp_path)
