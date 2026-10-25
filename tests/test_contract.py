import copy

from contract.validate import validate_label, validate_output

GOOD = {
    "msg_id": "m017", "intents": ["WISMO", "RETURN_REFUND"], "priority": "P1", "risk_flags": ["REFUND_REQUEST"],
    "route": "DRAFT_FOR_REVIEW",
    "tool_calls": [{"call_id": "c1", "name": "get_order", "args": {"order_id": "10231"}, "status": "ok"}],
    "reply": {"text": "...", "claims": [
        {"text": "Shipped on 26 September", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},
        {"text": "30 days from delivery", "source": {"type": "kb", "doc_id": "policy_returns", "lines": [5, 6]}}]},
    "handoff_note": "refund request",
    "meta": {"arch": "V0", "model": "x", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0, "latency_ms": 0, "run_id": "r1"},
}


def mutate(fn):
    o = copy.deepcopy(GOOD)
    fn(o)
    return validate_output(o)


def test_good_output_is_valid():
    assert validate_output(GOOD) == []


def test_escalate_may_have_null_reply_but_needs_handoff_note():
    assert mutate(lambda o: o.update(route="ESCALATE", reply=None)) == []
    assert mutate(lambda o: o.update(route="ESCALATE", reply=None, handoff_note=None))


def test_auto_and_draft_need_a_reply():
    assert mutate(lambda o: o.update(route="AUTO_REPLY", reply=None))
    assert mutate(lambda o: o.update(reply=None))


def test_schema_rejections():
    assert mutate(lambda o: o.update(intents=["WISMO", "OTHER", "COMPLAINT"]))
    assert mutate(lambda o: o.update(intents=[]))
    assert mutate(lambda o: o.update(risk_flags=["ANGRY"]))
    assert mutate(lambda o: o.update(extra_field=1))
    assert mutate(lambda o: o["reply"]["claims"][1]["source"].update(lines=[5]))
    assert mutate(lambda o: o["reply"]["claims"][1]["source"].update(type="web"))
    assert mutate(lambda o: o["meta"].pop("run_id"))


def test_semantic_rejections():
    assert mutate(lambda o: o["reply"]["claims"][1]["source"].update(lines=[6, 5]))
    assert mutate(lambda o: o["reply"]["claims"][0]["source"].update(call_id="c9"))
    assert mutate(lambda o: o["tool_calls"].append(dict(o["tool_calls"][0])))
    assert validate_output(GOOD, expected_msg_id="m018")


def test_tool_log_consistency():
    log = [{"call_id": "c1", "tool": "get_order", "args": {"order_id": "10231"}, "status": "ok"}]
    assert validate_output(GOOD, log) == []
    assert validate_output(GOOD, [])
    assert validate_output(GOOD, [{**log[0], "status": "timeout"}])


def test_label_validation():
    label = {"msg_id": "m017", "labeler": "zhang", "guideline_version": "v1", "intents": ["WISMO"], "priority": "P1",
             "risk_flags": [], "gold_route": "AUTO_REPLY", "expected_tool_calls": [{"name": "get_order", "args": {"order_id": "10231"}}],
             "gold_kb_spans": [{"doc_id": "policy_returns", "lines": [4, 6]}], "must_address": ["x"], "difficulty_tags": [], "note": ""}
    assert validate_label(label) == []
    bad = copy.deepcopy(label)
    bad["gold_kb_spans"][0]["lines"] = [6, 4]
    assert validate_label(bad)
