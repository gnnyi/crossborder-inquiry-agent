"""Architecture-agnostic scorer: contract outputs + tool logs + gold labels -> metrics.

    python scoring/score.py runs/V0/r1 runs/V0/r2 runs/V0/r3 --out reports/v0.md
    python scoring/score.py runs/V0/* runs/V1/* --split dev --details reports/details.jsonl

Expected run layout (see runners/README.md):
    runs/<arch>/<run_id>/run_meta.json          optional: arch, run_id, model, fault, ...
    runs/<arch>/<run_id>/outputs/<msg_id>.json  one contract output per message
    runs/<arch>/<run_id>/tool_logs/<msg_id>.jsonl
    runs/<arch>/<run_id>/errors/<msg_id>.json   {"msg_id", "error_type": "exception"|"timeout", "message"}

Only the deterministic metrics are computed here. The LLM-judge parts (fabrication checks 3 and 4,
must_address coverage) have an interface below and are reported as TODO.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from contract.validate import validate_label, validate_output  # noqa: E402
from sandbox import kb as kbmod  # noqa: E402
from sandbox.tools import read_log  # noqa: E402
from scoring.policy import intent_codes, risk_flags, route_levels  # noqa: E402
from scoring.route import derive_route  # noqa: E402

LABELS_DIR = REPO_ROOT / "eval" / "labels"
SPLITS_PATH = REPO_ROOT / "eval" / "splits.json"
AUTO = "AUTO_REPLY"
ESCALATE = "ESCALATE"
FAILED = "FAILED"


# =============================================================== LLM judge interface (TODO)
class LLMJudge(Protocol):
    """Not implemented in week 1. Validate against >=30 human judgements (>=90% agreement) before use."""

    def unsourced_claims(self, message: dict, output: dict) -> list[str]:
        """Fabrication check 3: facts in reply.text that no claim lists."""

    def unauthorised_commitments(self, message: dict, output: dict) -> list[str]:
        """Fabrication check 4: promises / policies not authorised by the KB or the risk policy."""

    def contradicts_tool_data(self, message: dict, output: dict, tool_log: list[dict]) -> list[str]:
        """Fabrication check 2, non-deterministic part (statuses, free-text facts)."""

    def must_address_coverage(self, message: dict, output: dict, must_address: list[str]) -> dict[str, bool]:
        """Which must_address points the reply covers."""


# =============================================================== field paths
_PATH_TOKEN = re.compile(r"\.?([^.\[\]]+)|\[(-?\d+)\]")


class _Missing:
    pass


MISSING = _Missing()


def resolve_field(obj: Any, path: str) -> Any:
    """Resolve 'shipments[0].events[-1].description' inside a tool result; MISSING if absent."""
    if not path:
        return MISSING
    pos, cur = 0, obj
    while pos < len(path):
        m = _PATH_TOKEN.match(path, pos)
        if not m or m.end() == pos:
            return MISSING
        key, idx = m.group(1), m.group(2)
        if key is not None:
            if pos == 0 and path.startswith("."):
                return MISSING
            if not isinstance(cur, dict) or key not in cur:
                return MISSING
            cur = cur[key]
        else:
            if not isinstance(cur, list):
                return MISSING
            i = int(idx)
            if not -len(cur) <= i < len(cur):
                return MISSING
            cur = cur[i]
        pos = m.end()
    return cur


# =============================================================== date / amount extraction
MONTHS = {
    # en
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # de
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "mai": 5, "juni": 6, "juli": 7,
    "oktober": 10, "okt": 10, "dezember": 12, "dez": 12,
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # fr
    "janvier": 1, "janv": 1, "février": 2, "fevrier": 2, "févr": 2, "mars": 3, "avril": 4, "avr": 4,
    "juin": 6, "juillet": 7, "juil": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12, "déc": 12,
}
_MONTH_ALT = "|".join(sorted((re.escape(k) for k in MONTHS), key=len, reverse=True))
_DATE_PATTERNS = [
    ("iso", re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})")),
    ("zh", re.compile(r"(?:(20\d\d)\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")),
    ("month_day", re.compile(rf"\b({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s*(20\d\d))?", re.I)),
    ("day_month", re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th|er|º|\.)?\s*(?:de\s+)?({_MONTH_ALT})\.?\b(?:\s*(?:de\s+)?(20\d\d))?", re.I)),
    ("numeric", re.compile(r"(?<![\d.,/])(\d{1,2})[./](\d{1,2})(?:[./](20\d\d|\d\d))?(?![\d/]|\.\d)")),
]


def extract_dates(text: str) -> list[set[tuple[int | None, int, int]]]:
    """Each date mention -> set of plausible (year|None, month, day) readings."""
    mentions: list[set[tuple[int | None, int, int]]] = []
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for kind, pat in _DATE_PATTERNS:
        for m in pat.finditer(text):
            if not free(m.span()):
                continue
            readings: set[tuple[int | None, int, int]] = set()
            if kind == "iso":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                readings.add((y, mo, d))
            elif kind == "zh":
                y = int(m.group(1)) if m.group(1) else None
                readings.add((y, int(m.group(2)), int(m.group(3))))
            elif kind == "month_day":
                mo = MONTHS[m.group(1).lower()]
                readings.add((int(m.group(3)) if m.group(3) else None, mo, int(m.group(2))))
            elif kind == "day_month":
                mo = MONTHS[m.group(2).lower()]
                readings.add((int(m.group(3)) if m.group(3) else None, mo, int(m.group(1))))
            else:
                a, b = int(m.group(1)), int(m.group(2))
                y = m.group(3)
                year = (int(y) if len(y) == 4 else 2000 + int(y)) if y else None
                readings.add((year, b, a))  # d/m (EU)
                readings.add((year, a, b))  # m/d (US)
            readings = {r for r in readings if 1 <= r[1] <= 12 and 1 <= r[2] <= 31}
            if readings:
                mentions.append(readings)
                taken.append(m.span())
    return mentions


_AMOUNT = r"(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
_AMOUNT_PATTERNS = [
    re.compile(rf"(?:US\$|USD|\$|€|£|EUR|GBP)\s?{_AMOUNT}", re.I),
    re.compile(rf"{_AMOUNT}\s?(?:US\$|USD|\$|€|£|EUR|GBP|dollars?|dólares|dollar\b|euros?)", re.I),
]


def _parse_number(s: str) -> float | None:
    s = s.replace(" ", "")
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    elif s.count(".") == 1 and len(s.rpartition(".")[2]) == 3 and len(s.rpartition(".")[0]) <= 3:
        s = s.replace(".", "")  # 1.234 (de thousands)
    try:
        return float(s)
    except ValueError:
        return None


def extract_amounts(text: str) -> list[float]:
    found: list[float] = []
    taken: list[tuple[int, int]] = []
    for pat in _AMOUNT_PATTERNS:
        for m in pat.finditer(text):
            if any(not (m.end() <= a or m.start() >= b) for a, b in taken):
                continue
            v = _parse_number(m.group(1))
            if v is not None:
                found.append(v)
                taken.append(m.span())
    return found


def _as_date(value: Any) -> tuple[dt.date, bool] | None:
    """(date, has_time) for ISO date / timestamp strings."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?", value)
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(1)), m.group(2) is not None
    except ValueError:
        return None


def check_claim_against_value(claim_text: str, value: Any) -> str | None:
    """Deterministic part of fabrication check 2. Returns a reason string on contradiction, else None.

    Dates: a timestamp field also accepts the neighbouring calendar day (time-zone conversion).
    Amounts: any currency-marked amount in the claim must equal the field (to the cent).
    Anything else (statuses, names) is left to the LLM judge.
    """
    as_date = _as_date(value)
    if as_date:
        mentions = extract_dates(claim_text)
        if not mentions:
            return None
        day, has_time = as_date
        ok_days = {day} | ({day - dt.timedelta(days=1), day + dt.timedelta(days=1)} if has_time else set())
        for readings in mentions:
            for y, mo, d in readings:
                for ok in ok_days:
                    if mo == ok.month and d == ok.day and (y is None or y == ok.year):
                        return None
        return f"date in claim does not match field value {value}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amounts = extract_amounts(claim_text)
        if not amounts:
            return None
        if any(abs(a - float(value)) < 0.005 for a in amounts):
            return None
        return f"amount(s) {amounts} in claim do not match field value {value}"
    return None


# =============================================================== loading
@dataclass
class Run:
    path: Path
    arch: str
    run_id: str
    meta: dict
    outputs: dict[str, Any] = field(default_factory=dict)        # msg_id -> parsed json (or _BadJSON)
    logs: dict[str, list[dict]] = field(default_factory=dict)
    errors: dict[str, dict] = field(default_factory=dict)


class _BadJSON:
    def __init__(self, err: str) -> None:
        self.err = err


def load_run(path: Path) -> Run:
    meta = {}
    if (path / "run_meta.json").exists():
        meta = json.loads((path / "run_meta.json").read_text(encoding="utf-8"))
    run = Run(path=path, arch=meta.get("arch") or path.parent.name, run_id=meta.get("run_id") or path.name, meta=meta)
    for f in sorted((path / "outputs").glob("*.json")):
        try:
            run.outputs[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            run.outputs[f.stem] = _BadJSON(str(e))
    for f in sorted((path / "tool_logs").glob("*.jsonl")):
        run.logs[f.stem] = read_log(f)
    for f in sorted((path / "errors").glob("*.json")):
        try:
            run.errors[f.stem] = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            run.errors[f.stem] = {"error_type": "exception", "message": "unreadable error file"}
    return run


def load_labels(labels_dir: Path, split: str | None = None, splits_path: Path = SPLITS_PATH) -> tuple[dict[str, dict], list[str]]:
    labels, warnings = {}, []
    for f in sorted(labels_dir.glob("*.json")):
        obj = json.loads(f.read_text(encoding="utf-8"))
        errs = validate_label(obj)
        if errs:
            warnings.append(f"label {f.name} skipped: {errs[0]}")
            continue
        derived = derive_route(obj["risk_flags"])
        if obj["gold_route"] != derived and not obj.get("route_override"):
            warnings.append(f"label {f.name}: gold_route {obj['gold_route']} != derived {derived} without route_override")
        labels[obj["msg_id"]] = obj
    if split:
        splits = json.loads(splits_path.read_text(encoding="utf-8"))
        keep = set(splits[split])
        labels = {k: v for k, v in labels.items() if k in keep}
    return labels, warnings


# =============================================================== per-message scoring
def _norm_args(name: str, args: Any) -> str:
    if not isinstance(args, dict):
        return json.dumps(args, sort_keys=True)
    a = dict(args)
    if name == "find_orders_by_email" and isinstance(a.get("email"), str):
        a["email"] = a["email"].strip().lower()
    return json.dumps(a, sort_keys=True, ensure_ascii=False)


def _spans_overlap(a: list[int], b: list[int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def score_message(msg_id: str, label: dict, run: Run, kb: dict[str, list[str]], failure_policy: str) -> dict:
    out = run.outputs.get(msg_id)
    log = run.logs.get(msg_id, [])
    row: dict[str, Any] = {"msg_id": msg_id, "gold_route": label["gold_route"], "gold_intents": label["intents"],
                           "gold_flags": label["risk_flags"], "gold_priority": label["priority"]}
    failure = None
    if msg_id in run.errors:
        failure = "timeout" if run.errors[msg_id].get("error_type") == "timeout" else "exception"
    elif out is None:
        failure = "missing_output"
    elif isinstance(out, _BadJSON):
        failure = "schema_invalid"
        row["schema_errors"] = [f"invalid JSON: {out.err}"]
    else:
        errs = validate_output(out, log, expected_msg_id=msg_id)
        if errs:
            failure = "schema_invalid"
            row["schema_errors"] = errs[:5]
    row["failure"] = failure

    if failure:
        row["pred_route"] = FAILED
        row["effective_route"] = ESCALATE if failure_policy == "human" else None
        row["pred_intents"], row["pred_flags"], row["pred_priority"] = [], [], None
        row["latency_ms"] = run.errors.get(msg_id, {}).get("latency_ms")
        row["cost_usd"] = run.errors.get(msg_id, {}).get("cost_usd", 0.0)
    else:
        row["pred_route"] = out["route"]
        row["effective_route"] = out["route"]
        row["pred_intents"], row["pred_flags"], row["pred_priority"] = out["intents"], out["risk_flags"], out["priority"]
        row["latency_ms"] = out["meta"]["latency_ms"]
        row["cost_usd"] = out["meta"]["cost_usd"]
        row["tokens_in"], row["tokens_out"] = out["meta"]["tokens_in"], out["meta"]["tokens_out"]

    # ---- tool calls (from the log: it is the ground truth of what was called)
    expected = [(c["name"], _norm_args(c["name"], c["args"])) for c in label["expected_tool_calls"]]
    made = [(r["tool"], _norm_args(r["tool"], r["args"])) for r in log]
    exp_set = set(expected)
    hit = {e for e in expected if e in made}
    row["expected_calls"] = len(exp_set)
    row["expected_calls_hit"] = len(hit)
    row["tool_all_expected"] = hit == exp_set
    row["extra_calls"] = sum(1 for m in made if m not in exp_set)
    row["repeat_calls"] = sum(c - 1 for m, c in Counter(made).items() if m in exp_set and c > 1)
    row["calls_made"] = len(made)
    row["call_statuses"] = dict(Counter(r["status"] for r in log))

    # ---- reply checks
    reply = None if failure else out["reply"]
    row["has_reply"] = reply is not None
    ghosts, contradictions = [], []
    kb_cites = []
    if reply:
        logged = {r["call_id"]: r for r in log}
        for i, claim in enumerate(reply["claims"]):
            src = claim["source"]
            if src["type"] == "kb":
                a, b = src["lines"]
                if src["doc_id"] not in kb:
                    ghosts.append(f"claim {i}: unknown doc_id {src['doc_id']}")
                elif not kbmod.is_valid_span(src["doc_id"], a, b, kb):
                    ghosts.append(f"claim {i}: {src['doc_id']} lines {a}-{b} out of range (1-{len(kb[src['doc_id']])})")
                else:
                    kb_cites.append((src["doc_id"], [a, b]))
            else:
                rec = logged.get(src["call_id"])
                if rec is None:
                    ghosts.append(f"claim {i}: call_id {src['call_id']} not in tool log")
                    continue
                if rec["status"] != "ok":
                    ghosts.append(f"claim {i}: call {src['call_id']} returned {rec['status']}, it has no fields")
                    continue
                value = resolve_field(rec["result"], src["field"])
                if value is MISSING:
                    ghosts.append(f"claim {i}: field {src['field']!r} not in result of {src['call_id']}")
                    continue
                reason = check_claim_against_value(claim["text"], value)
                if reason:
                    contradictions.append(f"claim {i} ({src['call_id']}.{src['field']}): {reason}")
    row["ghosts"] = ghosts
    row["contradictions"] = contradictions
    row["det_fabrication"] = bool(ghosts or contradictions)
    gold_spans = label["gold_kb_spans"]
    row["has_gold_spans"] = bool(gold_spans)
    row["citation_hit"] = any(doc == g["doc_id"] and _spans_overlap(lines, g["lines"]) for doc, lines in kb_cites for g in gold_spans) if gold_spans else None
    return row


# =============================================================== run-level metrics
def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def intent_f1(rows: list[dict]) -> tuple[float | None, dict[str, dict]]:
    per = {}
    codes = intent_codes()
    for code in codes:
        tp = sum(1 for r in rows if code in r["gold_intents"] and code in r["pred_intents"])
        fp = sum(1 for r in rows if code not in r["gold_intents"] and code in r["pred_intents"])
        fn = sum(1 for r in rows if code in r["gold_intents"] and code not in r["pred_intents"])
        if tp + fp + fn == 0:
            continue
        p = tp / (tp + fp) if tp + fp else 0.0
        rc = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
        per[code] = {"precision": p, "recall": rc, "f1": f1, "support": tp + fn}
    macro = sum(v["f1"] for v in per.values()) / len(per) if per else None
    return macro, per


NONE_COL = "(none)"


def intent_confusion(rows: list[dict]) -> dict[str, Counter]:
    """Multi-label confusion: matched codes on the diagonal; unmatched gold/pred codes are paired in
    sorted order, leftovers go to the (none) row/column."""
    m: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        g, p = set(r["gold_intents"]), set(r["pred_intents"])
        for c in g & p:
            m[c][c] += 1
        gl, pl = sorted(g - p), sorted(p - g)
        for a, b in zip(gl, pl):
            m[a][b] += 1
        for a in gl[len(pl):]:
            m[a][NONE_COL] += 1
        for b in pl[len(gl):]:
            m[NONE_COL][b] += 1
    return m


def run_metrics(rows: list[dict], run: Run, failure_policy: str) -> dict[str, Any]:
    n = len(rows)
    route_rows = [r for r in rows if r["effective_route"] is not None]
    gold_non_auto = [r for r in route_rows if r["gold_route"] != AUTO]
    gold_auto = [r for r in route_rows if r["gold_route"] == AUTO]
    gold_esc = [r for r in route_rows if r["gold_route"] == ESCALATE]
    ok_rows = [r for r in rows if not r["failure"]]
    replies = [r for r in ok_rows if r["has_reply"]]
    with_spans = [r for r in replies if r["has_gold_spans"]]
    tool_rows = [r for r in rows if r["expected_calls"]]
    macro, per_intent = intent_f1(rows)
    flags = risk_flags()
    per_flag = {}
    for f in flags:
        tp = sum(1 for r in rows if f in r["gold_flags"] and f in r["pred_flags"])
        fp = sum(1 for r in rows if f not in r["gold_flags"] and f in r["pred_flags"])
        fn = sum(1 for r in rows if f in r["gold_flags"] and f not in r["pred_flags"])
        if tp + fp + fn:
            per_flag[f] = {"precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn), "support": tp + fn}
    latencies = [r["latency_ms"] for r in rows if isinstance(r.get("latency_ms"), (int, float))]
    costs = [r["cost_usd"] or 0.0 for r in rows]
    failures = Counter(r["failure"] for r in rows if r["failure"])
    route_cm: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        route_cm[r["gold_route"]][r["pred_route"]] += 1
    call_status = Counter()
    for r in rows:
        call_status.update(r["call_statuses"])
    metrics = {
        "n_messages": n,
        "high_risk_capture": _ratio(sum(1 for r in gold_non_auto if r["effective_route"] != AUTO), len(gold_non_auto)),
        "escalate_capture": _ratio(sum(1 for r in gold_esc if r["effective_route"] == ESCALATE), len(gold_esc)),
        "false_block_rate": _ratio(sum(1 for r in gold_auto if r["effective_route"] != AUTO), len(gold_auto)),
        "route_accuracy": _ratio(sum(1 for r in route_rows if r["effective_route"] == r["gold_route"]), len(route_rows)),
        "auto_handled_upper_bound": _ratio(sum(1 for r in ok_rows if r["gold_route"] == AUTO and r["pred_route"] == AUTO and not r["det_fabrication"]), n),
        "intent_macro_f1": macro,
        "intent_exact_match": _ratio(sum(1 for r in rows if set(r["gold_intents"]) == set(r["pred_intents"])), n),
        "priority_accuracy": _ratio(sum(1 for r in rows if r["pred_priority"] == r["gold_priority"]), n),
        "risk_flags_exact_match": _ratio(sum(1 for r in rows if set(r["gold_flags"]) == set(r["pred_flags"])), n),
        "tool_call_accuracy": _ratio(sum(1 for r in tool_rows if r["tool_all_expected"]), len(tool_rows)),
        "tool_call_recall": _ratio(sum(r["expected_calls_hit"] for r in tool_rows), sum(r["expected_calls"] for r in tool_rows)),
        "extra_calls_total": sum(r["extra_calls"] for r in rows),
        "extra_calls_per_msg": _ratio(sum(r["extra_calls"] for r in rows), n),
        "repeat_calls_total": sum(r["repeat_calls"] for r in rows),
        "citation_recall": _ratio(sum(1 for r in with_spans if r["citation_hit"]), len(with_spans)),
        "ghost_citation_claims": sum(len(r["ghosts"]) for r in rows),
        "ghost_reply_rate": _ratio(sum(1 for r in replies if r["ghosts"]), len(replies)),
        "tool_contradiction_reply_rate": _ratio(sum(1 for r in replies if r["contradictions"]), len(replies)),
        "det_fabrication_rate": _ratio(sum(1 for r in replies if r["det_fabrication"]), len(replies)),
        "failure_rate": _ratio(sum(failures.values()), n),
        "cost_usd_total": sum(costs),
        "cost_usd_per_msg": _ratio(sum(costs), n) if n else None,
        "latency_p50_ms": _percentile(latencies, 0.5),
        "latency_p95_ms": _percentile(latencies, 0.95),
        # denominators, for honesty in the report
        "_den": {"gold_non_auto": len(gold_non_auto), "gold_auto": len(gold_auto), "gold_escalate": len(gold_esc),
                 "replies": len(replies), "replies_with_gold_spans": len(with_spans), "msgs_with_expected_calls": len(tool_rows),
                 "route_scored": len(route_rows)},
        "_failures": dict(failures),
        "_route_confusion": {k: dict(v) for k, v in route_cm.items()},
        "_intent_confusion": {k: dict(v) for k, v in intent_confusion(rows).items()},
        "_per_intent": per_intent,
        "_per_flag": per_flag,
        "_call_status": dict(call_status),
        "_fault": run.meta.get("fault"),
        "_unlabeled_outputs": sorted(set(run.outputs) - {r["msg_id"] for r in rows}),
        "_judge": "TODO: fabrication checks 3-4, must_address coverage and the auto-handled rate need the LLM judge",
    }
    return metrics


def score_run(run: Run, labels: dict[str, dict], kb: dict[str, list[str]], failure_policy: str = "human") -> tuple[dict, list[dict]]:
    rows = [score_message(mid, labels[mid], run, kb, failure_policy) for mid in sorted(labels)]
    return run_metrics(rows, run, failure_policy), rows


# =============================================================== aggregation + report
SCALARS = [
    ("high_risk_capture", "高风险捕获率", "≥ 90%", "pct"),
    ("escalate_capture", "ESCALATE 捕获率", "单独报告", "pct"),
    ("false_block_rate", "误拦率", "≤ 10%", "pct"),
    ("route_accuracy", "路由准确率", "", "pct"),
    ("auto_handled_upper_bound", "自动处理率（上界，未含判官项）", "越高越好", "pct"),
    ("det_fabrication_rate", "编造率（仅确定性项 1+2）", "接近 0", "pct"),
    ("ghost_reply_rate", "幽灵引用回复占比", "0", "pct"),
    ("ghost_citation_claims", "幽灵引用 claim 数", "0", "int"),
    ("tool_contradiction_reply_rate", "工具数据矛盾回复占比（日期/金额）", "0", "pct"),
    ("intent_macro_f1", "意图 macro-F1", "越高越好", "f3"),
    ("intent_exact_match", "意图完全一致", "", "pct"),
    ("priority_accuracy", "优先级准确率", "", "pct"),
    ("risk_flags_exact_match", "风险标签完全一致", "", "pct"),
    ("tool_call_accuracy", "工具调用准确率（消息级）", "越高越好", "pct"),
    ("tool_call_recall", "工具调用召回（调用级）", "", "pct"),
    ("extra_calls_per_msg", "多余调用 / 条", "越低越好", "f2"),
    ("repeat_calls_total", "重复调用数（含重试）", "", "int"),
    ("citation_recall", "引用召回", "越高越好", "pct"),
    ("failure_rate", "失败率", "越低越好", "pct"),
    ("cost_usd_per_msg", "成本 $/条", "越低越好", "usd"),
    ("cost_usd_total", "总成本 $", "", "usd"),
    ("latency_p50_ms", "延迟 p50 (ms)", "越低越好", "int"),
    ("latency_p95_ms", "延迟 p95 (ms)", "越低越好", "int"),
]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:.1%}"
    if kind == "f3":
        return f"{v:.3f}"
    if kind == "f2":
        return f"{v:.2f}"
    if kind == "usd":
        return f"{v:.4f}"
    return f"{v:.0f}"


def aggregate(metrics_list: list[dict]) -> dict[str, dict]:
    agg = {}
    for key, *_ in SCALARS:
        vals = [m[key] for m in metrics_list if m.get(key) is not None]
        agg[key] = {"mean": sum(vals) / len(vals) if vals else None, "min": min(vals) if vals else None,
                    "max": max(vals) if vals else None, "values": [m.get(key) for m in metrics_list]}
    return agg


def _matrix_md(matrix: dict[str, dict], rows_order: list[str], cols_order: list[str]) -> list[str]:
    rows_order = [r for r in rows_order if r in matrix] + sorted(r for r in matrix if r not in rows_order)
    present_cols = {c for r in matrix.values() for c in r}
    cols_order = [c for c in cols_order if c in present_cols] + sorted(c for c in present_cols if c not in cols_order)
    out = ["| 金标 \\ 预测 | " + " | ".join(cols_order) + " |", "|---|" + "---|" * len(cols_order)]
    for r in rows_order:
        out.append(f"| {r} | " + " | ".join(str(matrix[r].get(c, 0) or "·") for c in cols_order) + " |")
    return out


def _sum_matrices(ms: list[dict]) -> dict[str, dict]:
    total: dict[str, Counter] = defaultdict(Counter)
    for m in ms:
        for r, cols in m.items():
            total[r].update(cols)
    return {k: dict(v) for k, v in total.items()}


def render_report(results: dict[str, list[tuple[Run, dict]]], label_count: int, warnings: list[str], failure_policy: str, split: str | None) -> str:
    lines = ["# Eval 报告（确定性指标）", ""]
    lines.append(f"- 金标消息数：{label_count}" + (f"（split = {split}）" if split else ""))
    lines.append(f"- 失败处理：`{failure_policy}`" + ("（异常/超时/不合 schema 视为转人工，计入捕获率和误拦率）" if failure_policy == "human" else "（失败消息不计入路由指标）"))
    lines.append("- 编造检测第 3、4 项、must_address 覆盖需要 LLM 判官，本报告未实现（TODO）；自动处理率因此只是上界。")
    lines.append(f"- 样本量提示：{label_count} 条的量级下，几个百分点的差异很可能只是噪声。")
    lines.append("")
    for arch, items in sorted(results.items()):
        ms = [m for _, m in items]
        agg = aggregate(ms)
        lines.append(f"## {arch}（{len(items)} 次运行：{', '.join(r.run_id for r, _ in items)}）")
        lines.append("")
        faults = {json.dumps(m["_fault"], sort_keys=True) for m in ms if m["_fault"]}
        if faults:
            lines.append(f"- 故障注入：{'; '.join(sorted(faults))}")
        den = ms[0]["_den"]
        lines.append(f"- 分母（第一次运行）：金标非 AUTO {den['gold_non_auto']}，金标 AUTO {den['gold_auto']}，金标 ESCALATE {den['gold_escalate']}，"
                     f"有回复 {den['replies']}，有金标引用且有回复 {den['replies_with_gold_spans']}，有期望调用 {den['msgs_with_expected_calls']}")
        lines.append("")
        lines.append("| 指标 | 目标 | 均值 | 最小 | 最大 | 各次 |")
        lines.append("|---|---|---|---|---|---|")
        for key, title, target, kind in SCALARS:
            a = agg[key]
            per = " / ".join(_fmt(v, kind) for v in a["values"])
            lines.append(f"| {title} | {target} | {_fmt(a['mean'], kind)} | {_fmt(a['min'], kind)} | {_fmt(a['max'], kind)} | {per} |")
        lines.append("")
        fails = Counter()
        for m in ms:
            fails.update(m["_failures"])
        if fails:
            lines.append("失败明细（合计）：" + "，".join(f"{k} {v}" for k, v in sorted(fails.items())))
            lines.append("")
        cs = Counter()
        for m in ms:
            cs.update(m["_call_status"])
        if cs:
            lines.append("工具调用状态（合计）：" + "，".join(f"{k} {v}" for k, v in sorted(cs.items())))
            lines.append("")
        lines.append(f"### 路由混淆矩阵（{len(ms)} 次运行合计）")
        lines.append("")
        lines += _matrix_md(_sum_matrices([m["_route_confusion"] for m in ms]), route_levels(), route_levels() + [FAILED])
        lines.append("")
        lines.append(f"### 意图混淆矩阵（{len(ms)} 次运行合计；多标签：命中在对角线，未匹配的按字母序配对，多出的进 (none)）")
        lines.append("")
        lines += _matrix_md(_sum_matrices([m["_intent_confusion"] for m in ms]), intent_codes() + [NONE_COL], intent_codes() + [NONE_COL])
        lines.append("")
        lines.append("### 每个意图（第一次运行）")
        lines.append("")
        lines.append("| 意图 | P | R | F1 | 支持数 |")
        lines.append("|---|---|---|---|---|")
        for code, v in ms[0]["_per_intent"].items():
            lines.append(f"| {code} | {v['precision']:.2f} | {v['recall']:.2f} | {v['f1']:.2f} | {v['support']} |")
        lines.append("")
        lines.append("### 每个风险标签（第一次运行）")
        lines.append("")
        lines.append("| 标签 | 精确率 | 召回率 | 支持数 |")
        lines.append("|---|---|---|---|")
        for f, v in ms[0]["_per_flag"].items():
            lines.append(f"| {f} | {_fmt(v['precision'], 'pct')} | {_fmt(v['recall'], 'pct')} | {v['support']} |")
        lines.append("")
        unl = sorted({x for m in ms for x in m["_unlabeled_outputs"]})
        if unl:
            lines.append(f"未计分的输出（没有金标或不在 split 内）：{len(unl)} 条")
            lines.append("")
    if warnings:
        lines.append("## 警告")
        lines.append("")
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run directories runs/<arch>/<run_id>")
    ap.add_argument("--labels", default=str(LABELS_DIR))
    ap.add_argument("--split", choices=["dev", "test"], help="restrict to a split from eval/splits.json")
    ap.add_argument("--splits-file", default=str(SPLITS_PATH))
    ap.add_argument("--failure-policy", choices=["human", "exclude"], default="human")
    ap.add_argument("--out", help="write the markdown report here (default: stdout)")
    ap.add_argument("--json", help="write raw metrics JSON here")
    ap.add_argument("--details", help="write per-message rows (JSONL) here")
    args = ap.parse_args(argv)

    labels, warnings = load_labels(Path(args.labels), args.split, Path(args.splits_file))
    if not labels:
        print("no gold labels found — nothing to score", file=sys.stderr)
        return 2
    kb = kbmod.load_kb()
    results: dict[str, list[tuple[Run, dict]]] = defaultdict(list)
    details = []
    for rp in args.runs:
        run = load_run(Path(rp))
        metrics, rows = score_run(run, labels, kb, args.failure_policy)
        results[run.arch].append((run, metrics))
        details += [{"arch": run.arch, "run_id": run.run_id, **r} for r in rows]
    report = render_report(results, len(labels), warnings, args.failure_policy, args.split)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        print(f"report written to {args.out}")
    else:
        print(report)
    if args.json:
        Path(args.json).write_text(json.dumps({a: [{"run_id": r.run_id, **m} for r, m in v] for a, v in results.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.details:
        Path(args.details).write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in details), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
