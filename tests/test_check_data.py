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
