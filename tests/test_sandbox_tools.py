import json

import pytest

from sandbox.tools import FaultConfig, Sandbox, load_tool_schemas, read_log


def test_same_input_same_result_across_instances():
    a = Sandbox().get_order("10231")
    b = Sandbox().get_order("10231")
    assert a == b
    assert a["status"] == "ok" and a["result"]["order_id"] == "10231"


def test_results_are_deep_copies():
    sb = Sandbox()
    r1 = sb.get_order("10247")
    r1["result"]["items"][0]["unit_price"] = 0
    r1["result"]["shipments"].clear()
    r2 = sb.get_order("10247")
    assert r2["result"]["items"][0]["unit_price"] == 128.0
    assert r2["result"]["shipments"]
    assert r1["call_id"] == "c1" and r2["call_id"] == "c2"
    assert sb.log[0]["result"]["items"][0]["unit_price"] == 128.0


@pytest.mark.parametrize("raw", ["#10231", "NW-10231", "NW10231", " 10231", "10231 ", "1O231", "10231\n", 10231, None, ""])
def test_order_ids_are_not_repaired(raw):
    sb = Sandbox()
    assert sb.get_order(raw)["status"] == "invalid_argument"
    assert sb.get_tracking(raw)["status"] == "invalid_argument"


def test_not_found_and_other_validation():
    sb = Sandbox()
    assert sb.get_order("99999")["status"] == "not_found"
    assert sb.get_order("10200")["status"] == "not_found"
    assert sb.get_product("drs-004")["status"] == "invalid_argument"
    assert sb.get_product("DRS-099")["status"] == "not_found"
    assert sb.get_product("DRS-004")["result"]["price_usd"] == 118.0
    assert sb.find_orders_by_email("not-an-email")["status"] == "invalid_argument"
    assert sb.find_orders_by_email("nobody@example.com")["status"] == "not_found"
    assert sb.call("cancel_order", {"order_id": "10255"})["status"] == "invalid_argument"
    assert sb.call("get_order", {"order_id": "10231", "email": "x@example.com"})["status"] == "invalid_argument"
    assert sb.call("get_order", {})["status"] == "invalid_argument"


def test_find_orders_by_email_is_sorted_and_case_insensitive():
    r = Sandbox().find_orders_by_email("Olivia.Hart@example.com")
    ids = [o["order_id"] for o in r["result"]["orders"]]
    assert ids == ["10203", "10228", "10251"]
    assert r["result"]["email"] == "olivia.hart@example.com"


def test_tracking_matches_order():
    sb = Sandbox()
    t = sb.get_tracking("10236")["result"]
    assert t["order_status"] == "delivered"
    assert t["shipments"][0]["events"][-1]["code"] == "DELIVERED"
    assert sb.get_tracking("10255")["result"]["shipments"] == []


def test_log_lines(tmp_path):
    log = tmp_path / "m001.jsonl"
    sb = Sandbox(log_path=log)
    sb.get_order("10231")
    sb.get_order("#10231")
    rows = read_log(log)
    assert [r["call_id"] for r in rows] == ["c1", "c2"]
    for r in rows:
        assert {"call_id", "tool", "args", "status", "result", "simulated_latency_ms"} <= set(r)
    assert rows[0]["status"] == "ok" and rows[1]["status"] == "invalid_argument"
    assert rows[1]["args"] == {"order_id": "#10231"}


def _statuses(fault, calls):
    sb = Sandbox(fault=fault)
    return [(r := sb.call(name, args))["status"] for name, args in calls], sb.log


CALLS = [("get_order", {"order_id": str(i)}) for i in range(10201, 10261)] * 3


def test_faults_off_by_default():
    statuses, _ = _statuses(None, CALLS)
    assert set(statuses) == {"ok"}


def test_fault_injection_reproducible():
    fc = FaultConfig(seed=7, timeout_rate=0.2, not_found_rate=0.1)
    s1, log1 = _statuses(fc, CALLS)
    s2, log2 = _statuses(fc, CALLS)
    assert s1 == s2
    assert [r["simulated_latency_ms"] for r in log1] == [r["simulated_latency_ms"] for r in log2]
    assert "timeout" in s1 and "not_found" in s1
    # a different seed gives a different pattern
    s3, _ = _statuses(FaultConfig(seed=8, timeout_rate=0.2, not_found_rate=0.1), CALLS)
    assert s3 != s1


def test_retry_can_succeed():
    fc = FaultConfig(seed=7, timeout_rate=0.5)
    sb = Sandbox(fault=fc)
    for i in range(10201, 10261):
        first = sb.get_order(str(i))["status"]
        if first == "timeout":
            outcomes = [sb.get_order(str(i))["status"] for _ in range(10)]
            assert "ok" in outcomes
            return
    pytest.fail("expected at least one timeout at 50% rate")


def test_fault_depends_on_nth_call_not_on_other_calls():
    fc = FaultConfig(seed=3, timeout_rate=0.3, not_found_rate=0.2)
    a = Sandbox(fault=fc)
    b = Sandbox(fault=fc)
    b.get_product("DRS-001")  # unrelated call first
    assert a.get_order("10231")["status"] == b.get_order("10231")["status"]


def test_timeout_latency_and_injection_flag():
    sb = Sandbox(fault=FaultConfig(seed=1, timeout_rate=1.0))
    r = sb.get_order("10231")
    assert r["status"] == "timeout" and r["result"] is None
    assert sb.log[0]["simulated_latency_ms"] == 3000 and sb.log[0]["fault_injected"] is True
    # validation errors are never masked by injected faults
    assert sb.get_order("#10231")["status"] == "invalid_argument"


def test_tool_schemas_cover_all_tools():
    schemas = load_tool_schemas()
    assert [s["name"] for s in schemas] == ["get_order", "find_orders_by_email", "get_tracking", "get_product"]
    for s in schemas:
        assert s["input_schema"]["type"] == "object"
        assert s["input_schema"]["additionalProperties"] is False
    json.dumps(schemas)


def test_fault_config_validation():
    with pytest.raises(ValueError):
        FaultConfig(timeout_rate=0.8, not_found_rate=0.5)
