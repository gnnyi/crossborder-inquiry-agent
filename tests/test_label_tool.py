import builtins
import json
from pathlib import Path

import pytest

from tools import label as L


def _msgs():
    base = {"channel": "email", "lang": "en", "from": "jane.whitfield@example.com", "subject": "hi", "received_at": "2026-10-19T10:00:00Z"}
    return [{**base, "msg_id": "m901", "thread": [{"role": "customer", "text": "where is 10231", "ts": "2026-10-19T10:00:00Z"}]},
            {**base, "msg_id": "m902", "thread": [{"role": "customer", "text": "refund pls", "ts": "2026-10-19T10:00:00Z"}]}]


class Script:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"script ran out at prompt {prompt!r}")
        return self.answers.pop(0)


FULL = ["1 2", "1", "1", "", "o 10231; t 10231", "policy_returns:5-6", "包裹位置; 能否退款", ""]


def test_parsers():
    assert L.parse_tool_calls("o 10231; t 10231, e Jane@Example.com; p drs-001") == [
        {"name": "get_order", "args": {"order_id": "10231"}}, {"name": "get_tracking", "args": {"order_id": "10231"}},
        {"name": "find_orders_by_email", "args": {"email": "jane@example.com"}}, {"name": "get_product", "args": {"sku": "DRS-001"}}]
    assert L.parse_tool_calls("-") == []
    for bad in ["o #10231", "x 1", "o", "e notanemail", "p DRESS"]:
        with pytest.raises(L.ParseError):
            L.parse_tool_calls(bad)
    kb = L.kbmod.load_kb()
    assert L.parse_spans("policy_returns:4-6 faq:3", kb) == [{"doc_id": "policy_returns", "lines": [4, 6]}, {"doc_id": "faq", "lines": [3, 3]}]
    for bad in ["policy_returns:0-2", "policy_returns:6-4", "nope:1", "policy_returns:999"]:
        with pytest.raises(L.ParseError):
            L.parse_spans(bad, kb)
    opts = ["A", "B", "C"]
    assert L.parse_multi("3 1 3", opts) == ["C", "A"]
    with pytest.raises(L.ParseError):
        L.parse_multi("1 2 3", opts, max_items=2)
    assert L.parse_multi("", opts, allow_empty=True) == []
    assert L.parse_priority("p1") == "P1" and L.parse_priority("0") == "P0"
    assert L.parse_must("a; b；c") == ["a", "b", "c"]


def test_label_back_edit_and_resume(tmp_path):
    out = []
    sess = L.Session(_msgs(), tmp_path, "zhang", input_fn=Script([]), print_fn=out.append)
    first, second = sess.order
    script = Script(FULL + [""]            # first message, then save
                    + [":b"]               # at second message: go back
                    + [""] * 8 + [""]      # keep every default on the first message, save
                    + [":q"])              # quit at the second message
    sess.input = script
    sess.run()
    lab = json.loads((tmp_path / f"{first}.json").read_text())
    assert lab["intents"] == ["WISMO", "RETURN_REFUND"]
    assert lab["priority"] == "P1"
    assert lab["risk_flags"] == ["REFUND_REQUEST"]
    assert lab["gold_route"] == "DRAFT_FOR_REVIEW" and lab["route_override"] is False
    assert lab["expected_tool_calls"][1] == {"name": "get_tracking", "args": {"order_id": "10231"}}
    assert lab["gold_kb_spans"] == [{"doc_id": "policy_returns", "lines": [5, 6]}]
    assert lab["must_address"] == ["包裹位置", "能否退款"]
    assert lab["labeler"] == "zhang" and lab["guideline_version"] == "v1"
    assert not (tmp_path / f"{second}.json").exists()
    # resume starts at the unlabelled one
    sess2 = L.Session(_msgs(), tmp_path, "zhang", input_fn=Script([":q"]), print_fn=out.append)
    sess2.run()
    assert any(second in line for line in out[-20:] if isinstance(line, str))


def test_route_override_requires_note(tmp_path):
    out = []
    script = Script(["8", "2", "", "n", "3", "-", "-", "-", "", "spam that threatens staff", ""])
    sess = L.Session(_msgs()[:1], tmp_path, "zhang", input_fn=script, print_fn=out.append)
    sess.run()
    lab = json.loads((tmp_path / "m901.json").read_text())
    assert lab["gold_route"] == "ESCALATE" and lab["route_override"] is True
    assert lab["note"] == "spam that threatens staff"


def test_invalid_input_reprompts_and_lookup_commands(tmp_path):
    out = []
    script = Script([":o 10231", ":kb", ":kb faq", "1 2 3", "1", "1", "", "", "o #1", "-", "-", "-", "", ""])
    sess = L.Session(_msgs()[:1], tmp_path, "zhang", input_fn=script, print_fn=out.append)
    sess.run()
    text = "\n".join(out)
    assert '"order_id": "10231"' in text
    assert "KB docs:" in text and "- doc_id: faq" in text
    assert "at most 2" in text and "digits only" in text
    assert json.loads((tmp_path / "m901.json").read_text())["expected_tool_calls"] == []


def test_never_reads_sealed(tmp_path, monkeypatch):
    seen = []
    real_read_text, real_open = Path.read_text, builtins.open

    def spy_read_text(self, *a, **k):
        seen.append(str(self))
        return real_read_text(self, *a, **k)

    def spy_open(file, *a, **k):
        seen.append(str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(Path, "read_text", spy_read_text)
    monkeypatch.setattr(builtins, "open", spy_open)
    msgs = L.load_messages()
    sess = L.Session(msgs, tmp_path, "zhang", input_fn=Script([":o 10231", ":kb policy_returns", ":q"]), print_fn=lambda *_: None)
    sess.run()
    assert seen and not any("_sealed" in p for p in seen)
    with pytest.raises(SystemExit):
        L.load_messages(L.REPO_ROOT / "eval" / "_sealed" / "generation_notes.jsonl")


def test_label_order_is_fixed():
    msgs = L.load_messages()
    assert L.label_order(msgs) == L.label_order(list(reversed(msgs)))
    assert L.label_order(msgs)[:5] != sorted(m["msg_id"] for m in msgs)[:5]
