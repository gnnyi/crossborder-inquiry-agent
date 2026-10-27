"""Metrics that need no gold labels, for comparing runs before blind labelling is done.

    python scoring/label_free.py runs/V0-b/r1 runs/V1/r1 --out reports/label_free.md
    python scoring/label_free.py runs/V0-b/r1 --failures      # also writes <run>/label_free_failures.json

Nothing here reads eval/labels or eval/_sealed. The report only has run-level aggregates; the per-message
failure list goes into the run directory (for reading traces), never into the report.

Ghost citations and tool-data contradictions reuse scoring/score.py::score_message with a placeholder label,
so they are exactly the same checks the gold-label report runs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sandbox import kb as kbmod  # noqa: E402
from scoring.route import derive_route  # noqa: E402
from scoring.score import FAILED, Run, _percentile, extract_amounts, extract_dates, load_run, score_message  # noqa: E402

PLACEHOLDER_LABEL = {"gold_route": "AUTO_REPLY", "intents": [], "risk_flags": [], "priority": "P2",
                     "expected_tool_calls": [], "gold_kb_spans": []}
ORDER_ID_RE = re.compile(r"(?<![\d$.,])\b(1\d{4})\b(?![.,]\d)")
ROUTES = ["AUTO_REPLY", "DRAFT_FOR_REVIEW", "ESCALATE"]


# ------------------------------------------------------------------ unsourced facts (deterministic part)
def fact_mentions(text: str) -> dict[str, list]:
    """Dates (as sets of possible readings), currency amounts and order numbers mentioned in text."""
    return {"dates": extract_dates(text), "amounts": extract_amounts(text), "order_ids": ORDER_ID_RE.findall(text)}


def _same_day(a: tuple, b: tuple) -> bool:
    """(year|None, month, day) readings; a missing year matches any year."""
    return a[1:] == b[1:] and (a[0] is None or b[0] is None or a[0] == b[0])


def unsourced_facts(reply: dict) -> list[str]:
    """Facts in reply.text that no claim covers. A mention is covered when some claim's text mentions the
    same date (any shared reading), the same amount (to the cent) or the same order number."""
    if not reply:
        return []
    claim_texts = " \n ".join(c["text"] for c in reply.get("claims", []))
    have = fact_mentions(claim_texts)
    have_dates = set().union(*have["dates"]) if have["dates"] else set()
    missing = []
    found = fact_mentions(reply["text"])
    for readings in found["dates"]:
        if not any(_same_day(a, b) for a in readings for b in have_dates):
            missing.append(f"date {sorted(readings, key=str)[0]}")
    for a in found["amounts"]:
        if not any(abs(a - b) < 0.005 for b in have["amounts"]):
            missing.append(f"amount {a:.2f}")
    for oid in found["order_ids"]:
        if oid not in have["order_ids"]:
            missing.append(f"order {oid}")
    return missing


# ------------------------------------------------------------------ per message
def message_row(msg_id: str, run: Run, kb: dict) -> dict:
    row = score_message(msg_id, PLACEHOLDER_LABEL, run, kb, failure_policy="human")
    out = run.outputs.get(msg_id)
    log = run.logs.get(msg_id, [])
    ok = not row["failure"]
    meta = out["meta"] if ok else {}
    row["fallback"] = meta.get("handoff_reason") if meta.get("fallback") else None
    row["route"] = out["route"] if ok else FAILED
    row["unsourced"] = unsourced_facts(out["reply"]) if ok else []
    # route the model gave vs. route its own flags imply (only differs when the model chose the route)
    row["route_self_contradiction"] = bool(ok and not row["fallback"] and out["route"] != derive_route(out["risk_flags"]))
    keys = [(r["tool"], json.dumps(r["args"], sort_keys=True)) for r in log]
    row["invalid_arg_calls"] = sum(1 for r in log if r["status"] == "invalid_argument")
    row["not_found_calls"] = sum(1 for r in log if r["status"] == "not_found")
    row["identical_repeat_calls"] = sum(c - 1 for c in Counter(keys).values() if c > 1)
    row["faults_injected"] = sum(1 for r in log if r.get("fault_injected"))
    row["steps"] = meta.get("steps")
    row["llm_calls"] = meta.get("llm_calls")
    row["cache_read"] = meta.get("cache_read_tokens", 0)
    row["cache_write"] = meta.get("cache_write_tokens", 0)
    row["tokens_in_total"] = meta.get("tokens_in", 0)
    return row


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else None


def run_metrics(run: Run, msg_ids: list[str] | None = None) -> tuple[dict, list[dict]]:
    kb = kbmod.load_kb()
    ids = msg_ids or sorted(set(run.outputs) | set(run.errors))
    rows = [message_row(m, run, kb) for m in ids]
    n = len(rows)
    ok = [r for r in rows if not r["failure"]]
    agent_done = [r for r in ok if not r["fallback"]]
    replies = [r for r in ok if r["has_reply"]]
    failures = Counter(r["failure"] for r in rows if r["failure"])
    fallbacks = Counter(r["fallback"] for r in ok if r["fallback"])
    routes = Counter(r["route"] for r in rows)
    faulted = [r for r in rows if r["faults_injected"]]
    lat = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    cache_read = sum(r["cache_read"] for r in ok)
    tokens_in = sum(r["tokens_in_total"] for r in ok)
    calls_total = sum(r["calls_made"] for r in rows)
    m: dict[str, Any] = {
        "n": n,
        "contract_ok_rate": _ratio(len(ok), n),
        "agent_completed_rate": _ratio(len(agent_done), n),
        "failure_rate": _ratio(len(rows) - len(agent_done), n),
        "_failures": dict(failures),
        "_fallbacks": dict(fallbacks),
        "ghost_reply_rate": _ratio(sum(1 for r in replies if r["ghosts"]), len(replies)),
        "ghost_claims": sum(len(r["ghosts"]) for r in rows),
        "contradiction_reply_rate": _ratio(sum(1 for r in replies if r["contradictions"]), len(replies)),
        "unsourced_reply_rate": _ratio(sum(1 for r in replies if r["unsourced"]), len(replies)),
        "unsourced_mentions": sum(len(r["unsourced"]) for r in rows),
        "route_self_contradiction_rate": _ratio(sum(1 for r in agent_done if r["route_self_contradiction"]), len(agent_done)),
        "invalid_arg_calls": sum(r["invalid_arg_calls"] for r in rows),
        "not_found_calls": sum(r["not_found_calls"] for r in rows),
        "identical_repeat_calls": sum(r["identical_repeat_calls"] for r in rows),
        "tool_calls_per_msg": _ratio(calls_total, n),
        "avg_steps": _mean([r["steps"] for r in ok]),
        "llm_calls_per_msg": _mean([r["llm_calls"] for r in ok]),
        "route_share": {k: _ratio(routes.get(k, 0), n) for k in ROUTES + [FAILED]},
        "faulted_msgs": len(faulted),
        "fault_recovery_rate": _ratio(sum(1 for r in faulted if not r["failure"] and not r["fallback"]), len(faulted)),
        "cost_usd_total": sum((r["cost_usd"] or 0.0) for r in rows),
        "cost_usd_per_msg": _ratio(sum((r["cost_usd"] or 0.0) for r in rows), n),
        "latency_p50_ms": _percentile(lat, 0.5),
        "latency_p95_ms": _percentile(lat, 0.95),
        "cache_hit_rate": _ratio(cache_read, tokens_in),
        "_model": run.meta.get("model"),
        "_fault": run.meta.get("fault"),
    }
    return m, rows


def failure_index(rows: list[dict]) -> dict[str, list[str]]:
    """msg_ids per label-free failure category, for reading traces. Written next to the run, not in reports."""
    idx: dict[str, list[str]] = {}

    def add(cat: str, mid: str) -> None:
        idx.setdefault(cat, []).append(mid)

    for r in rows:
        if r["failure"]:
            add(f"failure:{r['failure']}", r["msg_id"])
        if r["fallback"]:
            add(f"handoff:{r['fallback']}", r["msg_id"])
        if r["ghosts"]:
            add("ghost_citation", r["msg_id"])
        if r["contradictions"]:
            add("tool_contradiction", r["msg_id"])
        if r["unsourced"]:
            add("unsourced_fact", r["msg_id"])
        if r["invalid_arg_calls"]:
            add("invalid_argument_call", r["msg_id"])
        if r["identical_repeat_calls"]:
            add("identical_repeat_call", r["msg_id"])
        if r["route_self_contradiction"]:
            add("route_self_contradiction", r["msg_id"])
    return idx


# ------------------------------------------------------------------ report
ROWS = [
    ("contract_ok_rate", "契约合规率（含被迫转人工）", "pct"),
    ("agent_completed_rate", "agent 自己完成的比例", "pct"),
    ("failure_rate", "失败率（异常/超时/不合契约/被迫转人工）", "pct"),
    ("ghost_reply_rate", "幽灵引用回复占比", "pct"),
    ("ghost_claims", "幽灵引用 claim 数", "int"),
    ("contradiction_reply_rate", "工具数据矛盾回复占比（日期/金额）", "pct"),
    ("unsourced_reply_rate", "含无来源事实的回复占比（日期/金额/订单号）", "pct"),
    ("unsourced_mentions", "无来源事实处数", "int"),
    ("route_self_contradiction_rate", "路由与自报风险标签不一致", "pct"),
    ("invalid_arg_calls", "非法参数调用数", "int"),
    ("not_found_calls", "not_found 调用数", "int"),
    ("identical_repeat_calls", "完全相同的重复调用数（含 timeout 重试）", "int"),
    ("tool_calls_per_msg", "工具调用 / 条", "f2"),
    ("avg_steps", "平均步数（模型调用轮数）", "f2"),
    ("llm_calls_per_msg", "模型调用 / 条（V2 含子 Agent）", "f2"),
    ("faulted_msgs", "被注入故障的消息数", "int"),
    ("fault_recovery_rate", "故障恢复率", "pct"),
    ("cost_usd_per_msg", "成本 $/条", "usd"),
    ("cost_usd_total", "总成本 $", "usd"),
    ("latency_p50_ms", "延迟 p50 (ms)", "int"),
    ("latency_p95_ms", "延迟 p95 (ms)", "int"),
    ("cache_hit_rate", "缓存命中率（缓存读 / 全部输入 tokens）", "pct"),
]


def fmt(v: Any, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "int":
        return f"{v:,.0f}"
    if kind == "usd":
        return f"${v:.4f}"
    return f"{v:.2f}"


def render(results: list[tuple[Run, dict]]) -> str:
    names = [f"{r.arch}/{r.run_id}" for r, _ in results]
    lines = ["# 无标签指标报告", "",
             "不读金标、不读 `_sealed`。只有汇总数字；逐条失败清单在各 run 目录的 `label_free_failures.json`。", "",
             "| 指标 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    lines += [f"| model | " + " | ".join(str(m["_model"]) for _, m in results) + " |",
              f"| 消息数 | " + " | ".join(str(m["n"]) for _, m in results) + " |",
              f"| 故障注入 | " + " | ".join(json.dumps(m["_fault"]) if m["_fault"] else "关" for _, m in results) + " |"]
    for key, label, kind in ROWS:
        lines.append(f"| {label} | " + " | ".join(fmt(m[key], kind) for _, m in results) + " |")
    lines += ["", "## 路由分布（汇总）", "", "| 路由 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for k in ROUTES + [FAILED]:
        lines.append(f"| {k} | " + " | ".join(fmt(m["route_share"][k], "pct") for _, m in results) + " |")
    lines += ["", "## 失败与被迫转人工的原因（条数）", "", "| 原因 | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    reasons = sorted({k for _, m in results for k in list(m["_failures"]) + list(m["_fallbacks"])})
    for k in reasons:
        lines.append(f"| {k} | " + " | ".join(str(m["_failures"].get(k, m["_fallbacks"].get(k, 0))) for _, m in results) + " |")
    if any(str(m["_model"]).startswith("fake") for _, m in results):
        lines += ["", "> ⚠️ 含 `model=fake` 的列是脚本模拟模型的管线冒烟结果，**不是**任何架构的真实表现。"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", help="write markdown here (default: stdout)")
    ap.add_argument("--failures", action="store_true", help="write <run>/label_free_failures.json")
    args = ap.parse_args(argv)
    results = []
    for p in args.runs:
        run = load_run(Path(p))
        m, rows = run_metrics(run)
        results.append((run, m))
        if args.failures:
            (Path(p) / "label_free_failures.json").write_text(json.dumps(failure_index(rows), indent=2))
    md = render(results)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md, encoding="utf-8")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
