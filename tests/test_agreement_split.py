import json

import pytest

from tools import agreement, split


def lab(mid, intents, flags, prio="P1", calls=None, spans=None):
    from scoring.route import derive_route
    return {"msg_id": mid, "labeler": "x", "guideline_version": "v1", "intents": intents, "priority": prio, "risk_flags": flags,
            "gold_route": derive_route(flags), "expected_tool_calls": calls or [], "gold_kb_spans": spans or [], "must_address": [],
            "difficulty_tags": [], "note": ""}


def write(d, labels):
    d.mkdir(parents=True, exist_ok=True)
    for l in labels:
        (d / f"{l['msg_id']}.json").write_text(json.dumps(l))


def test_agreement_rates_and_divergences(tmp_path, capsys):
    A = [lab("m001", ["WISMO"], []), lab("m002", ["RETURN_REFUND"], ["REFUND_REQUEST"]), lab("m003", ["OTHER"], ["INJECTION"]),
         lab("m004", ["WISMO", "COMPLAINT"], [], calls=[{"name": "get_order", "args": {"order_id": "10231"}}])]
    B = [lab("m001", ["WISMO"], []), lab("m002", ["RETURN_REFUND"], []), lab("m003", ["OTHER"], ["INJECTION"], prio="P2"),
         lab("m004", ["COMPLAINT", "WISMO"], [], calls=[{"name": "get_order", "args": {"order_id": "10231"}}]), lab("m005", ["OTHER"], [])]
    write(tmp_path / "a", A)
    write(tmp_path / "b", B)
    a, _ = agreement.load_side(tmp_path / "a")
    b, _ = agreement.load_side(tmp_path / "b")
    res = agreement.compare(a, b)
    assert res["n"] == 4 and res["only_b"] == 1
    assert res["fields"]["intents (exact set)"] == 1.0
    assert res["fields"]["risk_flags (exact set)"] == 0.75
    assert res["fields"]["route"] == 0.75
    assert res["fields"]["priority"] == 0.75
    assert res["per_flag"]["REFUND_REQUEST"]["a_count"] == 1 and res["per_flag"]["REFUND_REQUEST"]["b_count"] == 0
    assert [d[0] for d in res["divergences"]] == ["m002", "m003"]
    assert agreement.main([str(tmp_path / "a"), str(tmp_path / "b"), "--out", str(tmp_path / "r.md")]) == 0
    text = (tmp_path / "r.md").read_text()
    assert "分歧清单（2 条" in text and "### m002" in text


def test_kappa():
    assert agreement.cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    assert agreement.cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    assert agreement.cohen_kappa([], []) is None


def test_agreement_refuses_sealed_before_50_blind(tmp_path, capsys):
    write(tmp_path / "a", [lab("m001", ["WISMO"], [])])
    sealed = agreement.REPO_ROOT / "eval" / "_sealed" / "generation_notes.jsonl"
    assert agreement.main([str(tmp_path / "a"), str(sealed)]) == 2
    assert "at least 50" in capsys.readouterr().err
    assert agreement.main([str(tmp_path / "a"), str(sealed), "--force", "--out", str(tmp_path / "x.md")]) == 0


def _world(tmp_path, n=150):
    msgs, labels = [], []
    for i in range(1, n + 1):
        mid = f"m{i:03d}"
        turns = 6 if i % 9 == 0 else 1
        msgs.append({"msg_id": mid, "thread": [{"role": "customer", "text": "x", "ts": "2026-10-19T10:00:00Z"}] * turns})
        intents = ["WISMO", "COMPLAINT"] if i % 5 == 0 else ["WISMO"]
        flags = ["CHARGEBACK_LEGAL"] if i % 10 == 0 else (["REFUND_REQUEST"] if i % 3 == 0 else [])
        labels.append(lab(mid, intents, flags))
    write(tmp_path / "labels", labels)
    (tmp_path / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    return tmp_path / "labels", tmp_path / "messages.jsonl"


def test_split_is_stratified_deterministic_and_sealed(tmp_path, capsys):
    labels_dir, msgs_path = _world(tmp_path)
    labels, msgs = split.load(labels_dir, msgs_path)
    dev, test = split.make_split(labels, msgs, 50)
    assert len(dev) == 100 and len(test) == 50 and not set(dev) & set(test)
    assert (dev, test) == split.make_split(labels, msgs, 50)
    for ids in (dev, test):
        assert any(len(msgs[i]["thread"]) >= 5 for i in ids)
        assert any(len(labels[i]["intents"]) == 2 for i in ids)
    esc_test = sum(1 for i in test if labels[i]["gold_route"] == "ESCALATE")
    assert esc_test == 5  # 15 escalate overall -> a third in test
    out = tmp_path / "splits.json"
    args = ["--labels-dir", str(labels_dir), "--messages", str(msgs_path), "--out", str(out)]
    assert split.main(args) == 0
    assert "封存" in capsys.readouterr().out
    assert split.main(args) == 2                     # refuses to overwrite
    assert split.main(args + ["--verify"]) == 0
    t0 = json.loads(out.read_text())["test"][0]
    p = labels_dir / f"{t0}.json"
    p.write_text(p.read_text().replace('"note": ""', '"note": "edited"'))
    assert split.main(args + ["--verify"]) == 1


def test_split_requires_long_and_multi(tmp_path):
    labels_dir, msgs_path = _world(tmp_path, n=30)
    labels, msgs = split.load(labels_dir, msgs_path)
    for m in msgs.values():
        m["thread"] = m["thread"][:1]
    with pytest.raises(SystemExit):
        split.make_split(labels, msgs, 10)
