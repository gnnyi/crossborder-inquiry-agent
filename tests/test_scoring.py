import json
from pathlib import Path

import pytest

from sandbox import kb as kbmod
from sandbox.tools import FaultConfig, Sandbox
from scoring import score
from tests.fake_runners import load_fixture_labels, run_always_escalate, run_oracle

LABELS = load_fixture_labels()
KB = kbmod.load_kb()


def _score(run_dir, labels=LABELS, policy="human"):
    return score.score_run(score.load_run(run_dir), labels, KB, policy)


def test_fixture_labels_are_valid_and_not_in_eval_labels():
    from contract.validate import validate_label
    assert 5 <= len(LABELS) <= 10
    for lab in LABELS.values():
        assert validate_label(lab) == []
        for s in lab["gold_kb_spans"]:
            assert kbmod.is_valid_span(s["doc_id"], *s["lines"], KB)
    real = Path(score.LABELS_DIR)
    assert not any(real.glob("m9*.json"))


def test_oracle_gets_full_marks(tmp_path):
    m, rows = _score(run_oracle(LABELS, tmp_path / "ORACLE" / "r1"))
    assert m["high_risk_capture"] == 1.0
    assert m["escalate_capture"] == 1.0
    assert m["false_block_rate"] == 0.0
    assert m["route_accuracy"] == 1.0
    assert m["intent_macro_f1"] == 1.0
    assert m["priority_accuracy"] == 1.0
    assert m["risk_flags_exact_match"] == 1.0
    assert m["tool_call_accuracy"] == 1.0 and m["tool_call_recall"] == 1.0
    assert m["extra_calls_total"] == 0
    assert m["citation_recall"] == 1.0
    assert m["ghost_citation_claims"] == 0
    assert m["tool_contradiction_reply_rate"] == 0.0
    assert m["det_fabrication_rate"] == 0.0
    assert m["failure_rate"] == 0.0
    n_auto = sum(1 for lab in LABELS.values() if lab["gold_route"] == "AUTO_REPLY")
    assert m["auto_handled_upper_bound"] == n_auto / len(LABELS)
    assert all(not r["ghosts"] and not r["contradictions"] for r in rows)


def test_always_escalate_catches_everything_and_automates_nothing(tmp_path):
    m, _ = _score(run_always_escalate(LABELS, tmp_path / "ALWAYS_ESCALATE" / "r1"))
    assert m["high_risk_capture"] == 1.0
    assert m["escalate_capture"] == 1.0
    assert m["auto_handled_upper_bound"] == 0.0
    assert m["false_block_rate"] == 1.0
    assert m["failure_rate"] == 0.0


def _single_output(tmp_path, label, output, log_calls):
    run_dir = tmp_path / "X" / "r1"
    (run_dir / "outputs").mkdir(parents=True)
    sb = Sandbox(log_path=run_dir / "tool_logs" / f"{label['msg_id']}.jsonl")
    results = [{**sb.call(n, a), "args": a} for n, a in log_calls]
    output = output(results)
    (run_dir / "outputs" / f"{label['msg_id']}.json").write_text(json.dumps(output))
    m, rows = _score(run_dir, {label["msg_id"]: label})
    return m, rows[0]


def _base(label, claims, calls):
    return {"msg_id": label["msg_id"], "intents": label["intents"], "priority": label["priority"], "risk_flags": label["risk_flags"],
            "route": "AUTO_REPLY",
            "tool_calls": [{"call_id": c["call_id"], "name": c["tool"], "args": c.get("args", {"order_id": "10231"}), "status": c["status"]} for c in calls],
            "reply": {"text": "x", "claims": claims}, "handoff_note": None,
            "meta": {"arch": "X", "model": "m", "tokens_in": 1, "tokens_out": 1, "cost_usd": 0.0, "latency_ms": 5, "run_id": "r1"}}


def test_ghost_citations_are_detected(tmp_path):
    label = {**LABELS["m903"], "msg_id": "m950"}
    n = kbmod.line_count("policy_returns", KB)

    def out(calls):
        return _base(label, [
            {"text": "a", "source": {"type": "kb", "doc_id": "policy_refunds", "lines": [1, 2]}},          # unknown doc
            {"text": "b", "source": {"type": "kb", "doc_id": "policy_returns", "lines": [n, n + 3]}},       # out of range
            {"text": "c", "source": {"type": "tool", "call_id": "c1", "field": "shipped_on"}},             # missing field
            {"text": "d", "source": {"type": "tool", "call_id": "c1", "field": "items[5].sku"}},           # bad index
            {"text": "e", "source": {"type": "tool", "call_id": "c2", "field": "status"}},                 # call returned not_found
            {"text": "ok", "source": {"type": "tool", "call_id": "c1", "field": "items[0].sku"}},          # fine
            {"text": "ok", "source": {"type": "kb", "doc_id": "policy_returns", "lines": [5, 6]}},        # fine
        ], calls)
    m, row = _single_output(tmp_path, label, out, [("get_order", {"order_id": "10231"}), ("get_order", {"order_id": "99999"})])
    assert row["failure"] is None
    assert len(row["ghosts"]) == 5, row["ghosts"]
    assert m["ghost_reply_rate"] == 1.0
    assert m["det_fabrication_rate"] == 1.0
    assert row["citation_hit"] is True
    assert row["extra_calls"] == 1  # get_order(99999) was not expected


def test_ghost_call_id_not_in_log(tmp_path):
    label = {**LABELS["m903"], "msg_id": "m951"}

    def out(calls):
        o = _base(label, [{"text": "x", "source": {"type": "tool", "call_id": "c1", "field": "status"}}], calls)
        return o
    run_dir = tmp_path / "X" / "r1"
    (run_dir / "outputs").mkdir(parents=True)
    (run_dir / "tool_logs").mkdir(parents=True)
    o = out([{"call_id": "c1", "tool": "get_order", "status": "ok"}])
    (run_dir / "outputs" / "m951.json").write_text(json.dumps(o))
    (run_dir / "tool_logs" / "m951.jsonl").write_text("")
    m, rows = _score(run_dir, {"m951": label})
    # output claims a call the log never saw: contract check fails -> counted as a failure, not silently scored
    assert rows[0]["failure"] == "schema_invalid"
    assert m["failure_rate"] == 1.0


def test_tool_data_contradictions(tmp_path):
    label = {**LABELS["m903"], "msg_id": "m952"}
    order = Sandbox().get_order("10231")["result"]
    shipped = order["shipped_at"][:10]   # 2026-09-26

    def out(calls):
        return _base(label, [
            {"text": "Your order shipped on October 3.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},      # wrong
            {"text": "Your order shipped on September 26.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},   # right
            {"text": "Ihre Bestellung wurde am 26. September versandt.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},
            {"text": "Enviado el 26 de septiembre de 2026.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},
            {"text": "Expédiée le 25 septembre.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},             # tz-neighbour, accepted
            {"text": "您的订单已于 10 月 3 日发货", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},            # wrong
            {"text": "The total was $170.00.", "source": {"type": "tool", "call_id": "c1", "field": "total"}},                      # wrong
            {"text": "Le total était de 180,00 $.", "source": {"type": "tool", "call_id": "c1", "field": "total"}},                 # right
            {"text": "Your order has shipped.", "source": {"type": "tool", "call_id": "c1", "field": "shipped_at"}},               # no date: not checked
        ], calls)
    assert shipped == "2026-09-26" and order["total"] == 180.0
    m, row = _single_output(tmp_path, label, out, [("get_order", {"order_id": "10231"})])
    assert len(row["contradictions"]) == 3, row["contradictions"]
    assert m["tool_contradiction_reply_rate"] == 1.0
    assert not row["ghosts"]


@pytest.mark.parametrize("text,value,bad", [
    ("Delivered 10/06", "2026-10-06T14:22:00Z", False),
    ("Delivered 06/10/2026", "2026-10-06T14:22:00Z", False),
    ("Delivered 16.10.", "2026-10-06T14:22:00Z", True),
    ("Geliefert am 6. Okt.", "2026-10-06T14:22:00Z", False),
    ("Delivered Oct 9", "2026-10-06", True),
    ("Delivered Oct 7", "2026-10-06", True),     # plain date field: no tz tolerance
    ("Refund of US$120.05 issued", 120.05, False),
    ("Refund of 120,05 € issued", 120.05, False),
    ("Refund of $1,200.00", 1200.0, False),
    ("Refund of $128", 120.05, True),
    ("2 items in this order", 2, False),
])
def test_claim_value_check(text, value, bad):
    assert (score.check_claim_against_value(text, value) is not None) is bad


def test_field_paths():
    obj = {"a": [{"b": 1}, {"b": 2}], "c": {"d": None}}
    assert score.resolve_field(obj, "a[1].b") == 2
    assert score.resolve_field(obj, "a[-1].b") == 2
    assert score.resolve_field(obj, "c.d") is None
    for bad in ["a[2].b", "x", "a.b", "c..d", ".c", "", "a[0]["]:
        assert score.resolve_field(obj, bad) is score.MISSING


def test_failures_are_counted(tmp_path):
    run_dir = run_oracle(LABELS, tmp_path / "ORACLE" / "r1")
    ids = sorted(LABELS)
    (run_dir / "outputs" / f"{ids[0]}.json").unlink()                                  # missing
    (run_dir / "outputs" / f"{ids[1]}.json").write_text("{not json")                    # invalid json
    (run_dir / "errors").mkdir()
    (run_dir / "errors" / f"{ids[2]}.json").write_text(json.dumps({"msg_id": ids[2], "error_type": "timeout", "message": "120s"}))
    m, rows = _score(run_dir)
    assert m["failure_rate"] == 3 / len(LABELS)
    assert m["_failures"] == {"missing_output": 1, "schema_invalid": 1, "timeout": 1}
    m2, _ = _score(run_dir, policy="exclude")
    assert m2["_den"]["route_scored"] == len(LABELS) - 3


def test_extra_and_repeat_calls(tmp_path):
    fault = FaultConfig(seed=11, timeout_rate=0.5)
    label = {**LABELS["m901"], "msg_id": "m953"}

    def out(calls):
        o = _base(label, [], calls)
        o["tool_calls"] = [{"call_id": c["call_id"], "name": c["tool"], "args": a, "status": c["status"]} for c, a in zip(calls, ARGS)]
        return o
    ARGS = [{"order_id": "10245"}, {"order_id": "10245"}, {"order_id": "#10245"}]
    run_dir = tmp_path / "X" / "r1"
    (run_dir / "outputs").mkdir(parents=True)
    sb = Sandbox(log_path=run_dir / "tool_logs" / "m953.jsonl", fault=fault)
    calls = [sb.call("get_tracking", a) for a in ARGS]
    (run_dir / "outputs" / "m953.json").write_text(json.dumps(out(calls)))
    m, rows = _score(run_dir, {"m953": label})
    assert rows[0]["tool_all_expected"] is True
    assert rows[0]["repeat_calls"] == 1
    assert rows[0]["extra_calls"] == 1


def test_multi_run_aggregation_and_report(tmp_path):
    runs = [run_oracle(LABELS, tmp_path / "V0" / "r1", arch="V0", run_id="r1"),
            run_oracle(LABELS, tmp_path / "V0" / "r2", arch="V0", run_id="r2"),
            run_always_escalate(LABELS, tmp_path / "V0" / "r3", arch="V0", run_id="r3")]
    out = tmp_path / "report.md"
    det = tmp_path / "details.jsonl"
    labels_dir = tmp_path / "labels"
    labels_dir.mkdir()
    for k, v in LABELS.items():
        (labels_dir / f"{k}.json").write_text(json.dumps(v))
    rc = score.main([str(r) for r in runs] + ["--labels", str(labels_dir), "--out", str(out), "--details", str(det), "--json", str(tmp_path / "m.json")])
    assert rc == 0
    text = out.read_text()
    assert "## V0（3 次运行：r1, r2, r3）" in text
    assert "| 高风险捕获率 | ≥ 90% | 100.0% | 100.0% | 100.0% |" in text
    n_auto = sum(1 for lab in LABELS.values() if lab["gold_route"] == "AUTO_REPLY")
    fb = f"| 误拦率 | ≤ 10% | {1 / 3:.1%} | 0.0% | 100.0% |"
    assert fb in text
    assert "路由混淆矩阵" in text and "意图混淆矩阵" in text
    assert len(det.read_text().splitlines()) == 3 * len(LABELS)
    metrics = json.loads((tmp_path / "m.json").read_text())
    assert [r["run_id"] for r in metrics["V0"]] == ["r1", "r2", "r3"]
    assert n_auto > 0


def test_label_route_mismatch_is_warned(tmp_path):
    d = tmp_path / "labels"
    d.mkdir()
    lab = {**LABELS["m903"], "gold_route": "AUTO_REPLY"}
    (d / "m903.json").write_text(json.dumps(lab))
    labels, warnings = score.load_labels(d)
    assert "m903" in labels and any("derived" in w for w in warnings)
