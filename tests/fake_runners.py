"""Sanity-check runners used only by tests. Real V0/V1/V2 runners live in runners/ and are written by hand."""
from __future__ import annotations

import json
from pathlib import Path

from sandbox import kb as kbmod
from sandbox.tools import FaultConfig, Sandbox

FIXTURE_LABELS = Path(__file__).resolve().parent / "fixtures" / "labels"


def load_fixture_labels() -> dict[str, dict]:
    return {p.stem: json.loads(p.read_text()) for p in sorted(FIXTURE_LABELS.glob("*.json"))}


def _write(run_dir: Path, arch: str, run_id: str, outputs: dict[str, dict], fault: FaultConfig | None = None) -> Path:
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "tool_logs").mkdir(parents=True, exist_ok=True)
    meta = {"arch": arch, "run_id": run_id, "model": "none"}
    if fault and fault.enabled:
        meta["fault"] = fault.to_dict()
    (run_dir / "run_meta.json").write_text(json.dumps(meta))
    for mid, out in outputs.items():
        (run_dir / "outputs" / f"{mid}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return run_dir


def _meta(arch: str, run_id: str, i: int) -> dict:
    return {"arch": arch, "model": "none", "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.001, "latency_ms": 1000 + 10 * i, "run_id": run_id}


def _tool_claim(r: dict) -> dict | None:
    if r["status"] != "ok":
        return None
    res = r["result"]
    if r["tool"] == "get_order":
        return {"text": f"Your order was placed on {res['created_at'][:10]} and its total was ${res['total']:.2f}.",
                "source": {"type": "tool", "call_id": r["call_id"], "field": "created_at"}}
    if r["tool"] == "get_tracking":
        ev = res["shipments"][0]["events"][-1]
        return {"text": f"Latest scan: {ev['description']}", "source": {"type": "tool", "call_id": r["call_id"], "field": "shipments[0].events[-1].description"}}
    if r["tool"] == "get_product":
        return {"text": f"It is currently ${res['price_usd']:.2f}.", "source": {"type": "tool", "call_id": r["call_id"], "field": "price_usd"}}
    if r["tool"] == "find_orders_by_email":
        return {"text": f"I can see {len(res['orders'])} orders on this email.", "source": {"type": "tool", "call_id": r["call_id"], "field": "orders"}}
    return None


def run_oracle(labels: dict[str, dict], run_dir: Path, arch: str = "ORACLE", run_id: str = "r1", fault: FaultConfig | None = None) -> Path:
    """Copies the gold label into the contract output, makes exactly the expected calls and cites the gold spans."""
    outputs = {}
    for i, (mid, lab) in enumerate(sorted(labels.items())):
        sb = Sandbox(log_path=run_dir / "tool_logs" / f"{mid}.jsonl", fault=fault)
        calls = [sb.call(c["name"], c["args"]) for c in lab["expected_tool_calls"]]
        claims = []
        for span in lab["gold_kb_spans"]:
            a, b = span["lines"]
            claims.append({"text": " ".join(kbmod.get_lines(span["doc_id"], a, b)), "source": {"type": "kb", "doc_id": span["doc_id"], "lines": [a, b]}})
        for c in calls:
            tc = _tool_claim(c)
            if tc:
                claims.append(tc)
        route = lab["gold_route"]
        reply = None if route == "ESCALATE" else {"text": "Hi, " + " ".join(c["text"] for c in claims) or "Hi! How can we help?", "claims": claims}
        outputs[mid] = {
            "msg_id": mid, "intents": lab["intents"], "priority": lab["priority"], "risk_flags": lab["risk_flags"], "route": route,
            "tool_calls": [{"call_id": c["call_id"], "name": c["tool"], "args": sb.log[k]["args"], "status": c["status"]} for k, c in enumerate(calls)],
            "reply": reply, "handoff_note": None if route == "AUTO_REPLY" else "oracle: see gold risk flags",
            "meta": _meta(arch, run_id, i),
        }
    return _write(run_dir, arch, run_id, outputs, fault)


def run_always_escalate(labels: dict[str, dict], run_dir: Path, arch: str = "ALWAYS_ESCALATE", run_id: str = "r1") -> Path:
    outputs = {}
    for i, mid in enumerate(sorted(labels)):
        (run_dir / "tool_logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "tool_logs" / f"{mid}.jsonl").write_text("")
        outputs[mid] = {"msg_id": mid, "intents": ["OTHER"], "priority": "P2", "risk_flags": [], "route": "ESCALATE",
                        "tool_calls": [], "reply": None, "handoff_note": "everything goes to a human", "meta": _meta(arch, run_id, i)}
    return _write(run_dir, arch, run_id, outputs)
