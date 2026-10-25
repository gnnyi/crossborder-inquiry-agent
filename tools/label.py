"""Command-line labelling tool. Writes eval/labels/<msg_id>.json.

    .venv/bin/python tools/label.py                 # resume where you stopped (fixed random order)
    .venv/bin/python tools/label.py --goto m042     # jump to one message
    .venv/bin/python tools/label.py --review        # walk through already-labelled messages too

At any prompt:
    :s  skip this message        :b  back to the previous message     :q  save nothing and quit
    :g m042  go to a message     :o 10231 / :t 10231 / :e a@b.com / :p DRS-001   look up the sandbox
    :kb  list KB docs            :kb policy_returns   show a doc with line numbers
    Enter  keep the value shown in [brackets] (when editing an existing label)

This tool never reads the sealed directory (AI pre-labels), so your labels stay blind.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import re
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from contract.validate import validate_label  # noqa: E402
from sandbox import kb as kbmod  # noqa: E402
from sandbox.tools import Sandbox  # noqa: E402
from scoring.policy import PRIORITIES, intent_codes, risk_flags, risk_policy, route_levels  # noqa: E402
from scoring.route import derive_route, priority_floor  # noqa: E402

MESSAGES = REPO_ROOT / "eval" / "messages.jsonl"
LABELS_DIR = REPO_ROOT / "eval" / "labels"
ORDER_SEED = 20261021
GUIDELINE_VERSION = "v1"
SHORTHAND = {"o": ("get_order", "order_id"), "t": ("get_tracking", "order_id"),
             "e": ("find_orders_by_email", "email"), "p": ("get_product", "sku")}
REVERSE = {v[0]: k for k, v in SHORTHAND.items()}


class Nav(Exception):
    """Raised from a prompt to move around: kind in {skip, back, quit, goto}."""

    def __init__(self, kind: str, target: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.target = target


class ParseError(ValueError):
    pass


def _guard(path: Path) -> Path:
    if "_sealed" in Path(path).resolve().parts:
        raise SystemExit("refusing to read the sealed directory from the labelling tool")
    return path


# ------------------------------------------------------------------ parsers (pure, tested)
def _numbers(text: str) -> list[int]:
    parts = [p for p in re.split(r"[\s,]+", text.strip()) if p]
    if not all(p.isdigit() for p in parts):
        raise ParseError("use numbers separated by spaces, e.g. 1 3")
    return [int(p) for p in parts]


def parse_multi(text: str, options: list[str], max_items: int | None = None, allow_empty: bool = False) -> list[str]:
    if text.strip() in {"-", ""}:
        if allow_empty:
            return []
        raise ParseError("pick at least one")
    nums = _numbers(text)
    if any(n < 1 or n > len(options) for n in nums):
        raise ParseError(f"numbers must be 1-{len(options)}")
    picked = []
    for n in nums:
        if options[n - 1] not in picked:
            picked.append(options[n - 1])
    if max_items and len(picked) > max_items:
        raise ParseError(f"at most {max_items}")
    return picked


def parse_priority(text: str) -> str:
    t = text.strip().upper()
    if t in {"0", "1", "2"}:
        t = "P" + t
    if t not in PRIORITIES:
        raise ParseError("enter 0, 1 or 2 (P0 / P1 / P2)")
    return t


def parse_tool_calls(text: str) -> list[dict]:
    """'o 10231; t 10231, e a@b.com; p DRS-001' -> expected_tool_calls. '-' = none."""
    if text.strip() in {"", "-"}:
        return []
    calls = []
    for chunk in [c.strip() for c in re.split(r"[;,]", text) if c.strip()]:
        bits = chunk.split()
        if len(bits) != 2 or bits[0].lower() not in SHORTHAND:
            raise ParseError(f"bad tool shorthand {chunk!r}: use o/t/e/p followed by one argument")
        name, field = SHORTHAND[bits[0].lower()]
        arg = bits[1]
        if field == "order_id" and not re.fullmatch(r"[0-9]+", arg):
            raise ParseError(f"order id must be digits only (write the normalised id the agent should call): {arg!r}")
        if field == "email":
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", arg):
                raise ParseError(f"bad email {arg!r}")
            arg = arg.lower()
        if field == "sku":
            arg = arg.upper()
            if not re.fullmatch(r"[A-Z]{3}-[0-9]{3}", arg):
                raise ParseError(f"bad SKU {arg!r}")
        call = {"name": name, "args": {field: arg}}
        if call not in calls:
            calls.append(call)
    return calls


def format_tool_calls(calls: list[dict]) -> str:
    return "; ".join(f"{REVERSE[c['name']]} {next(iter(c['args'].values()))}" for c in calls) or "-"


def parse_spans(text: str, kb: dict[str, list[str]]) -> list[dict]:
    """'policy_returns:4-6 discount_rules:15' -> gold_kb_spans, validated against the KB."""
    if text.strip() in {"", "-"}:
        return []
    spans = []
    for tok in [t for t in re.split(r"[\s,;]+", text.strip()) if t]:
        m = re.fullmatch(r"([a-z0-9_]+):(\d+)(?:-(\d+))?", tok)
        if not m:
            raise ParseError(f"bad span {tok!r}: use doc_id:start-end")
        doc, a, b = m.group(1), int(m.group(2)), int(m.group(3) or m.group(2))
        if doc not in kb:
            raise ParseError(f"unknown doc_id {doc!r} (:kb lists them)")
        if not kbmod.is_valid_span(doc, a, b, kb):
            raise ParseError(f"{doc} has lines 1-{len(kb[doc])}; {a}-{b} is invalid")
        span = {"doc_id": doc, "lines": [a, b]}
        if span not in spans:
            spans.append(span)
    return spans


def format_spans(spans: list[dict]) -> str:
    return " ".join(f"{s['doc_id']}:{s['lines'][0]}-{s['lines'][1]}" for s in spans) or "-"


def _required(text: str) -> str:
    if not text.strip():
        raise ParseError("please give a reason")
    return text.strip()


def parse_must(text: str) -> list[str]:
    if text.strip() in {"", "-"}:
        return []
    return [p.strip() for p in re.split(r"[;；]", text) if p.strip()]


# ------------------------------------------------------------------ io helpers
def load_messages(path: Path = MESSAGES) -> list[dict]:
    _guard(path)
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def label_order(msgs: list[dict], seed: int = ORDER_SEED) -> list[str]:
    ids = sorted(m["msg_id"] for m in msgs)
    random.Random(seed).shuffle(ids)
    return ids


def load_label(labels_dir: Path, msg_id: str) -> dict | None:
    p = _guard(labels_dir) / f"{msg_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def render_message(m: dict) -> str:
    lines = [f"{m['msg_id']}  ·  {m['channel']}  ·  lang={m['lang']}  ·  from={m['from']}  ·  received {m['received_at']}"]
    if "subject" in m:
        lines.append(f"subject: {m['subject']!r}")
    for i, t in enumerate(m["thread"], 1):
        who = "CUSTOMER" if t["role"] == "customer" else "AGENT   "
        lines.append(f"\n--- [{i}/{len(m['thread'])}] {who} {t['ts']} ---")
        lines.append(t["text"] if t["text"].strip() else "(empty)")
    return "\n".join(lines)


class Session:
    def __init__(self, msgs: list[dict], labels_dir: Path, labeler: str, input_fn: Callable[[str], str] = input,
                 print_fn: Callable[[str], None] = print, seed: int = ORDER_SEED) -> None:
        self.msgs = {m["msg_id"]: m for m in msgs}
        self.order = label_order(msgs, seed)
        self.labels_dir = _guard(labels_dir)
        self.labels_dir.mkdir(parents=True, exist_ok=True)
        self.labeler = labeler
        self.input = input_fn
        self.print = print_fn
        self.kb = kbmod.load_kb()
        self.sandbox = Sandbox()  # lookups for the labeller only; nothing is logged

    # -------------------------------------------------- commands available at every prompt
    def _command(self, raw: str) -> bool:
        cmd = raw.strip()
        if cmd == ":s":
            raise Nav("skip")
        if cmd == ":b":
            raise Nav("back")
        if cmd == ":q":
            raise Nav("quit")
        if cmd.startswith(":g "):
            raise Nav("goto", cmd[3:].strip())
        if cmd == ":kb":
            self.print("KB docs: " + ", ".join(f"{d} ({len(v)} lines)" for d, v in sorted(self.kb.items())))
            return True
        if cmd.startswith(":kb "):
            doc = cmd[4:].strip()
            try:
                self.print(kbmod.render_with_line_numbers(doc, self.kb))
            except kbmod.KBError as e:
                self.print(str(e))
            return True
        m = re.fullmatch(r":([otep])\s+(\S+)", cmd)
        if m:
            name, field = SHORTHAND[m.group(1)]
            r = self.sandbox.call(name, {field: m.group(2)})
            self.print(json.dumps(r["result"], ensure_ascii=False, indent=1) if r["status"] == "ok" else f"{r['status']}: {r['error']}")
            return True
        return False

    def ask(self, prompt: str, parse: Callable[[str], object], default: object | None = None, default_text: str | None = None) -> object:
        while True:
            suffix = f" [{default_text}]" if default is not None and default_text is not None else ""
            raw = self.input(f"{prompt}{suffix}: ")
            if raw.strip().startswith(":") and self._command(raw):
                continue
            if raw.strip() == "" and default is not None:
                return default
            try:
                return parse(raw)
            except ParseError as e:
                self.print(f"  ! {e}")

    # -------------------------------------------------- one message
    def label_one(self, msg_id: str) -> dict | None:
        m = self.msgs[msg_id]
        old = load_label(self.labels_dir, msg_id)
        done = sum(1 for x in self.order if (self.labels_dir / f"{x}.json").exists())
        self.print("\n" + "=" * 78)
        self.print(f"[{self.order.index(msg_id) + 1}/{len(self.order)}]  labelled so far: {done}" + ("   (editing existing label)" if old else ""))
        self.print(render_message(m))
        self.print("=" * 78)
        intents = intent_codes()
        flags = risk_flags()
        self.print("intents: " + "  ".join(f"{i}={c}" for i, c in enumerate(intents, 1)))
        lab_intents = self.ask("intents (1-2 numbers)", lambda t: parse_multi(t, intents, max_items=2),
                               old and old["intents"], old and " ".join(str(intents.index(x) + 1) for x in old["intents"]))
        lab_priority = self.ask("priority (0/1/2)", parse_priority, old and old["priority"], old and old["priority"])
        self.print("risk flags: " + "  ".join(f"{i}={c}" for i, c in enumerate(flags, 1)) + "   (Enter or - = none)")
        old_flags_text = (" ".join(str(flags.index(x) + 1) for x in old["risk_flags"]) or "-") if old else None
        lab_flags = self.ask("risk_flags", lambda t: parse_multi(t, flags, allow_empty=True),
                             old["risk_flags"] if old else [], old_flags_text if old else None)
        derived = derive_route(lab_flags)
        floor = priority_floor(lab_flags)
        if floor == "P0" and lab_priority != "P0":
            self.print(f"  ! policy: {', '.join(f for f in lab_flags if f in risk_policy()['priority']['p0_flags'])} means P0, you chose {lab_priority}")
        self.print(f"  derived gold_route = {derived}")
        route, override = derived, False
        ok = self.ask("accept derived route? (Y/n)", lambda t: t.strip().lower() in {"", "y", "yes"}, True, "Y")
        if not ok:
            levels = route_levels()
            self.print("routes: " + "  ".join(f"{i}={r}" for i, r in enumerate(levels, 1)))
            route = self.ask("gold_route", lambda t: parse_multi(t, levels, max_items=1)[0])
            override = route != derived
        lab_tools = self.ask("expected_tool_calls (o 10231; t 10231; e a@b.com; p SKU; - none)", parse_tool_calls,
                             old and old["expected_tool_calls"], old and format_tool_calls(old["expected_tool_calls"]))
        lab_spans = self.ask("gold_kb_spans (policy_returns:4-6 ...; - none)", lambda t: parse_spans(t, self.kb),
                             old and old["gold_kb_spans"], old and format_spans(old["gold_kb_spans"]))
        lab_must = self.ask("must_address (separate with ;)", parse_must, old and old["must_address"],
                            old and ("; ".join(old["must_address"]) or "-"))
        note_default = old["note"] if old else ""
        lab_note = self.ask("note", lambda t: t.strip(), note_default if old else None, note_default if old else None)
        if override and not lab_note:
            lab_note = self.ask("note (required when overriding the route: why?)", _required)
        label = {
            "msg_id": msg_id, "labeler": self.labeler, "guideline_version": GUIDELINE_VERSION,
            "intents": lab_intents, "priority": lab_priority, "risk_flags": lab_flags, "gold_route": route,
            "route_override": override, "expected_tool_calls": lab_tools, "gold_kb_spans": lab_spans,
            "must_address": lab_must, "difficulty_tags": old["difficulty_tags"] if old else [], "note": lab_note,
            "labeled_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        errs = validate_label(label)
        if errs:
            self.print("  ! label invalid, not saved: " + "; ".join(errs))
            return None
        self.print(json.dumps({k: v for k, v in label.items() if k not in {"labeler", "guideline_version", "labeled_at"}}, ensure_ascii=False))
        if not self.ask("save? (Y/n)", lambda t: t.strip().lower() in {"", "y", "yes"}, True, "Y"):
            return None
        (self.labels_dir / f"{msg_id}.json").write_text(json.dumps(label, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.print(f"  saved eval/labels/{msg_id}.json")
        return label

    # -------------------------------------------------- loop
    def run(self, start: str | None = None, review: bool = False) -> None:
        if start:
            if start not in self.msgs:
                self.print(f"unknown msg_id {start}")
                return
            pos = self.order.index(start)
        else:
            pos = next((i for i, x in enumerate(self.order) if review or not (self.labels_dir / f"{x}.json").exists()), None)
            if pos is None:
                self.print("all messages are labelled. Use --review to edit, or --goto m042.")
                return
        history: list[int] = []
        while pos is not None:
            msg_id = self.order[pos]
            try:
                self.label_one(msg_id)
                history.append(pos)
                pos = self._next(pos, review)
            except Nav as nav:
                if nav.kind == "quit":
                    self.print("bye — progress is saved per message; run again to resume.")
                    return
                if nav.kind == "skip":
                    history.append(pos)
                    pos = self._next(pos, review)
                elif nav.kind == "back":
                    pos = history.pop() if history else max(pos - 1, 0)
                elif nav.kind == "goto":
                    if nav.target in self.msgs:
                        history.append(pos)
                        pos = self.order.index(nav.target)
                    else:
                        self.print(f"unknown msg_id {nav.target}")
        self.print("reached the end of the queue.")

    def _next(self, pos: int, review: bool) -> int | None:
        for i in range(pos + 1, len(self.order)):
            if review or not (self.labels_dir / f"{self.order[i]}.json").exists():
                return i
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labeler", default="zhang")
    ap.add_argument("--messages", default=str(MESSAGES))
    ap.add_argument("--labels-dir", default=str(LABELS_DIR))
    ap.add_argument("--goto", help="start at this msg_id")
    ap.add_argument("--review", action="store_true", help="also visit messages that already have a label")
    ap.add_argument("--seed", type=int, default=ORDER_SEED, help="labelling order seed (keep fixed)")
    args = ap.parse_args(argv)
    msgs = load_messages(Path(args.messages))
    Session(msgs, Path(args.labels_dir), args.labeler, seed=args.seed).run(args.goto, args.review)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\ninterrupted — saved labels are kept.")
        sys.exit(130)
