import json
import shutil

from tools import check_data


def test_check_data_passes_on_repo_data(capsys):
    assert check_data.main([]) == 0
    assert "0 error(s)" in capsys.readouterr().out


def _mutated_env(tmp_path, monkeypatch, mutate):
    env = tmp_path / "env"
    shutil.copytree(check_data.ENV, env)
    orders = json.loads((env / "orders.json").read_text())
    mutate(orders)
    (env / "orders.json").write_text(json.dumps(orders))
    monkeypatch.setattr(check_data, "ENV", env)


def test_detects_wrong_total(tmp_path, monkeypatch, capsys):
    def mutate(orders):
        orders[30]["total"] += 1
    _mutated_env(tmp_path, monkeypatch, mutate)
    assert check_data.main([]) == 1
    assert "total" in capsys.readouterr().out


def test_detects_unknown_sku_and_bad_email(tmp_path, monkeypatch, capsys):
    def mutate(orders):
        orders[0]["items"][0]["sku"] = "DRS-999"
        orders[1]["email"] = "real.person@gmail.com"
    _mutated_env(tmp_path, monkeypatch, mutate)
    assert check_data.main([]) == 1
    out = capsys.readouterr().out
    assert "unknown sku DRS-999" in out and "reserved example domain" in out


def test_detects_date_order_problem(tmp_path, monkeypatch, capsys):
    def mutate(orders):
        o = next(o for o in orders if o["order_id"] == "10231")
        o["shipments"][0]["delivered_at"] = "2026-09-01T00:00:00Z"
    _mutated_env(tmp_path, monkeypatch, mutate)
    assert check_data.main([]) == 1
    assert "10231" in capsys.readouterr().out


def test_order_token_extraction_folds_letter_o():
    assert check_data.order_tokens("order #1O231 and NW-10247, also nw10255") == ["10231", "10247", "10255"]
    assert check_data.order_tokens("call me at 555-0100") == []


def _messages_env(tmp_path, monkeypatch, msgs, notes):
    ev = tmp_path / "eval"
    (ev / "_sealed").mkdir(parents=True)
    (ev / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    (ev / "_sealed" / "generation_notes.jsonl").write_text("".join(json.dumps(n) + "\n" for n in notes))
    monkeypatch.setattr(check_data, "EVAL", ev)


def _msg(text, **extra):
    m = {"msg_id": "m001", "channel": "email", "lang": "en", "from": "jane.whitfield@example.com", "subject": "hi",
         "received_at": "2026-10-19T10:00:00Z", "thread": [{"role": "customer", "text": text, "ts": "2026-10-19T10:00:00Z"}]}
    m.update(extra)
    return m


def _note(**refs):
    base = {"orders": [], "bad_order_refs": [], "non_order_numbers": [], "sender_matches_order": None, "sender_has_orders": None}
    base.update(refs)
    return {"msg_id": "m001", "refs": base, "expected_tool_calls": []}


def test_message_checks_pass_for_declared_refs(tmp_path, monkeypatch, capsys):
    _messages_env(tmp_path, monkeypatch, [_msg("where is #1O231? zip 27608")],
                  [_note(orders=["10231"], non_order_numbers=["27608"], sender_matches_order=True)])
    assert check_data.main([]) == 0


def test_message_checks_catch_undeclared_order_and_leaks(tmp_path, monkeypatch, capsys):
    _messages_env(tmp_path, monkeypatch, [_msg("order 10231 and 10299", intents=["WISMO"])], [_note()])
    assert check_data.main([]) == 1
    out = capsys.readouterr().out
    assert "possible label leak" in out
    assert "10231 not declared" in out
    assert "10299 neither exists nor is declared" in out


def test_message_checks_catch_sender_mismatch_claim(tmp_path, monkeypatch, capsys):
    _messages_env(tmp_path, monkeypatch, [_msg("order 10213 please")], [_note(orders=["10213"], sender_matches_order=True)])
    assert check_data.main([]) == 1
    assert "sender matches" in capsys.readouterr().out


def test_repo_messages_have_no_label_fields():
    msgs = [json.loads(x) for x in (check_data.EVAL / "messages.jsonl").read_text().splitlines() if x.strip()]
    assert len(msgs) >= 150
    for m in msgs:
        assert set(m) <= check_data.MSG_KEYS
