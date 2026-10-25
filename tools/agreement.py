"""Agreement between two label sets.

    .venv/bin/python tools/agreement.py eval/labels eval/_sealed/generation_notes.jsonl --out reports/agree-vs-ai.md
    .venv/bin/python tools/agreement.py eval/labels path/to/relabel-3-days-later --out reports/self-agreement.md

Each side is either a directory of label files (<msg_id>.json) or the sealed generation_notes.jsonl
(its AI pre-labels). Only messages present on both sides are compared. The divergence list at the end
is the candidate list for new 判例 in eval/guideline.md.

Comparing against the sealed pre-labels is refused until the label directory holds at least 50 labels
(blind-label first), unless --force.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scoring.policy import risk_flags  # noqa: E402
from scoring.route import derive_route  # noqa: E402

MESSAGES = REPO_ROOT / "eval" / "messages.jsonl"
MIN_BLIND = 50


def load_side(path: Path) -> tuple[dict[str, dict], str]:
    if path.is_dir():
        out = {}
        for f in sorted(path.glob("*.json")):
            lab = json.loads(f.read_text(encoding="utf-8"))
            out[lab["msg_id"]] = {k: lab.get(k) for k in ["intents", "priority", "risk_flags", "gold_route", "expected_tool_calls", "gold_kb_spans", "must_address"]}
        return out, "labels"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        n = json.loads(line)
        p = n["pre_label"]
        out[n["msg_id"]] = {"intents": p["intents"], "priority": p["priority"], "risk_flags": p["risk_flags"],
                            "gold_route": derive_route(p["risk_flags"]), "expected_tool_calls": p["expected_tool_calls"],
                            "gold_kb_spans": None, "must_address": p.get("must_address")}
    return out, "pre_labels"


def cohen_kappa(a: list, b: list) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum(ca[k] * cb[k] for k in set(ca) | set(cb)) / (n * n)
    if pe == 1:
        return 1.0 if po == 1 else 0.0
    return (po - pe) / (1 - pe)


def _calls_key(calls: list[dict] | None) -> frozenset:
    return frozenset(json.dumps(c, sort_keys=True) for c in (calls or []))


def _spans_overlap(a: list[dict], b: list[dict]) -> bool:
    if not a and not b:
        return True
    return any(x["doc_id"] == y["doc_id"] and x["lines"][0] <= y["lines"][1] and y["lines"][0] <= x["lines"][1] for x in a for y in b)


def compare(A: dict[str, dict], B: dict[str, dict]) -> dict:
    ids = sorted(set(A) & set(B))
    res: dict = {"n": len(ids), "only_a": len(set(A) - set(B)), "only_b": len(set(B) - set(A)), "fields": {}, "per_flag": {}, "divergences": []}
    if not ids:
        return res

    def rate(fn) -> float:
        return sum(1 for i in ids if fn(A[i], B[i])) / len(ids)

    f = res["fields"]
    f["intents (exact set)"] = rate(lambda a, b: set(a["intents"]) == set(b["intents"]))
    f["intents (Jaccard mean)"] = sum(len(set(A[i]["intents"]) & set(B[i]["intents"])) / len(set(A[i]["intents"]) | set(B[i]["intents"])) for i in ids) / len(ids)
    f["priority"] = rate(lambda a, b: a["priority"] == b["priority"])
    f["priority κ"] = cohen_kappa([A[i]["priority"] for i in ids], [B[i]["priority"] for i in ids])
    f["risk_flags (exact set)"] = rate(lambda a, b: set(a["risk_flags"]) == set(b["risk_flags"]))
    f["route"] = rate(lambda a, b: a["gold_route"] == b["gold_route"])
    f["route κ"] = cohen_kappa([A[i]["gold_route"] for i in ids], [B[i]["gold_route"] for i in ids])
    f["route (AUTO vs not)"] = rate(lambda a, b: (a["gold_route"] == "AUTO_REPLY") == (b["gold_route"] == "AUTO_REPLY"))
    f["expected_tool_calls (exact set)"] = rate(lambda a, b: _calls_key(a["expected_tool_calls"]) == _calls_key(b["expected_tool_calls"]))
    both_spans = [i for i in ids if A[i].get("gold_kb_spans") is not None and B[i].get("gold_kb_spans") is not None]
    if both_spans:
        f["gold_kb_spans (overlap)"] = sum(1 for i in both_spans if _spans_overlap(A[i]["gold_kb_spans"], B[i]["gold_kb_spans"])) / len(both_spans)
    for flag in risk_flags():
        a = [flag in A[i]["risk_flags"] for i in ids]
        b = [flag in B[i]["risk_flags"] for i in ids]
        if any(a) or any(b):
            res["per_flag"][flag] = {"agreement": sum(1 for x, y in zip(a, b) if x == y) / len(ids), "kappa": cohen_kappa(a, b),
                                     "a_count": sum(a), "b_count": sum(b)}
    for i in ids:
        diffs = {}
        for key in ["intents", "risk_flags"]:
            if set(A[i][key]) != set(B[i][key]):
                diffs[key] = (A[i][key], B[i][key])
        for key in ["priority", "gold_route"]:
            if A[i][key] != B[i][key]:
                diffs[key] = (A[i][key], B[i][key])
        if _calls_key(A[i]["expected_tool_calls"]) != _calls_key(B[i]["expected_tool_calls"]):
            diffs["expected_tool_calls"] = (A[i]["expected_tool_calls"], B[i]["expected_tool_calls"])
        if diffs:
            res["divergences"].append((i, diffs))
    return res


def _calls_text(calls: list[dict]) -> str:
    short = {"get_order": "o", "get_tracking": "t", "find_orders_by_email": "e", "get_product": "p"}
    return "; ".join(f"{short[c['name']]} {next(iter(c['args'].values()))}" for c in calls) or "-"


def render(res: dict, name_a: str, name_b: str, messages: dict[str, dict]) -> str:
    out = [f"# 标注一致率：A = `{name_a}`，B = `{name_b}`", "",
           f"共同消息 {res['n']} 条（只在 A：{res['only_a']}，只在 B：{res['only_b']}）。", ""]
    if not res["n"]:
        return "\n".join(out + ["没有可比较的消息。"])
    out += ["| 字段 | 一致率 |", "|---|---|"]
    for k, v in res["fields"].items():
        out.append(f"| {k} | {'—' if v is None else (f'{v:.3f}' if 'κ' in k else f'{v:.1%}')} |")
    out += ["", "## 每个风险标签", "", "| 标签 | 一致率 | κ | A 打了 | B 打了 |", "|---|---|---|---|---|"]
    for flag, v in res["per_flag"].items():
        k = "—" if v["kappa"] is None else f"{v['kappa']:.2f}"
        out.append(f"| {flag} | {v['agreement']:.1%} | {k} | {v['a_count']} | {v['b_count']} |")
    out += ["", f"## 分歧清单（{len(res['divergences'])} 条，判例候选）", ""]
    for mid, diffs in res["divergences"]:
        m = messages.get(mid)
        snippet = ""
        if m:
            last = m["thread"][-1]["text"].strip().replace("\n", " ")
            snippet = (last[:140] + "…") if len(last) > 140 else last
        out.append(f"### {mid}" + (f" — {m['lang']} · {m['channel']} · {len(m['thread'])} 轮" if m else ""))
        if snippet:
            out.append(f"> {snippet}")
        out.append("")
        for k, (a, b) in diffs.items():
            if k == "expected_tool_calls":
                a, b = _calls_text(a), _calls_text(b)
            out.append(f"- **{k}**：A `{a}` · B `{b}`")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out")
    ap.add_argument("--force", action="store_true", help="compare with sealed pre-labels even with fewer than 50 blind labels")
    args = ap.parse_args(argv)
    A, kind_a = load_side(Path(args.a))
    B, kind_b = load_side(Path(args.b))
    if "pre_labels" in {kind_a, kind_b} and not args.force:
        n_blind = len(A) if kind_a == "labels" else len(B)
        if n_blind < MIN_BLIND:
            print(f"only {n_blind} blind labels — label at least {MIN_BLIND} before comparing with the AI pre-labels (or pass --force)", file=sys.stderr)
            return 2
    messages = {}
    if MESSAGES.exists():
        messages = {m["msg_id"]: m for m in (json.loads(x) for x in MESSAGES.read_text(encoding="utf-8").splitlines() if x.strip())}
    report = render(compare(A, B), args.a, args.b, messages)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
