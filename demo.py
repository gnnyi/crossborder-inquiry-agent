"""Print one complete trace in the terminal: every model decision, tool call, result, the final output and route.

    python demo.py m003 --arch V0                      # live run with the real model
    python demo.py m003 --arch V1 --fake               # scripted model, no API call
    python demo.py m003 --replay runs/V0-b/v0b-r1      # print a trace already on disk, no API call

Live runs write to runs/_demo/<arch>/<timestamp>/ (never overwritten).
Until you have blind-labelled at least 100 messages, live and replay modes print the agent's triage of a
real eval message, which can anchor your labels; they refuse to run unless you pass --allow-before-labels.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from pathlib import Path

from dotenv import load_dotenv

from agents.common import RUNS_DIR, RunDir, get_message
from agents.llm import FakeLLM, LLM, Meter, has_api_key
from sandbox.tools import Sandbox

LABELS_DIR = Path(__file__).resolve().parent / "eval" / "labels"
W = 100
BOLD, DIM, CYAN, YEL, GRN, RED, END = "\033[1m", "\033[2m", "\033[36m", "\033[33m", "\033[32m", "\033[31m", "\033[0m"


def short(obj, n: int = 300) -> str:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def hr(title: str) -> None:
    print(f"\n{BOLD}{'─' * 3} {title} {'─' * max(0, W - len(title) - 5)}{END}")


def print_entries(entries: list[dict], indent: str = "") -> None:
    for e in entries:
        t = e.get("type")
        where = e.get("node") or (f"step {e['step']}" if "step" in e else "")
        if t == "model":
            print(f"{indent}{CYAN}[{where}] model{END} stop={e.get('stop_reason', '')}")
            if e.get("text"):
                print(textwrap.indent(textwrap.fill(short(e["text"], 400), W - len(indent) - 4), indent + "    " + DIM) + END)
            for u in e.get("tool_uses", []):
                print(f"{indent}    → {BOLD}{u['name']}{END} {short(u['input'], 220)}")
            for err in e.get("errors") or []:
                print(f"{indent}    {RED}✗ {short(err, 200)}{END}")
        elif t == "tool":
            col = GRN if e["status"] == "ok" else YEL if e["status"] == "timeout" else RED
            print(f"{indent}{YEL}[{where}] tool{END} {e['name']} {short(e['input'], 120)} → {col}{e.get('call_id') or ''} {e['status']}{END}"
                  + (f" ({e['error']})" if e.get("error") else ""))
        elif t == "submit":
            ok = not e["errors"]
            print(f"{indent}{GRN if ok else RED}[{where}] submit {'accepted' if ok else 'rejected'}{END}")
            for err in e["errors"]:
                print(f"{indent}    {RED}✗ {short(err, 200)}{END}")
        elif t == "nudge":
            print(f"{indent}{YEL}[{where}] nudge: no tool call, asked once more{END}")
        elif t == "handoff":
            print(f"{indent}{RED}[{where}] HANDOFF → human: {e['reason']}{END}")
        elif t == "gate":
            print(f"{indent}{BOLD}[risk_gate] route = {e['route']}{END} (derived by code from risk_flags)")
        elif t == "orchestrator":
            print(f"{indent}{BOLD}orchestrator loop{END}")
            print_entries(e["trace"], indent + "  ")
        elif t == "worker":
            print(f"{indent}{BOLD}┌ worker: {e['worker']}{END}  brief={short(e['brief'], 200)}")
            print_entries(e["trace"], indent + "│ ")
            print(f"{indent}└ returned: {short(e['returned'], 300)}" + (f" {RED}(handoff: {e['handoff']}){END}" if e.get("handoff") else ""))


def print_output(out: dict) -> None:
    hr("final output")
    print(f"intents={out['intents']}  priority={out['priority']}  risk_flags={out['risk_flags']}")
    col = GRN if out["route"] == "AUTO_REPLY" else YEL if out["route"] == "DRAFT_FOR_REVIEW" else RED
    print(f"route = {col}{BOLD}{out['route']}{END}" + ("  (fallback: " + out["meta"]["handoff_reason"] + ")" if out["meta"].get("fallback") else ""))
    if out["reply"]:
        print(f"\n{BOLD}reply{END}\n" + textwrap.indent(textwrap.fill(out["reply"]["text"], W - 4), "    "))
        print(f"\n{BOLD}claims{END}")
        for c in out["reply"]["claims"]:
            src = c["source"]
            where = f"{src['doc_id']}:{src['lines'][0]}-{src['lines'][1]}" if src["type"] == "kb" else f"{src['call_id']}.{src['field']}"
            print(f"    • {short(c['text'], 120)}  {DIM}[{where}]{END}")
    if out["handoff_note"]:
        print(f"\n{BOLD}handoff_note{END}: {out['handoff_note']}")
    m = out["meta"]
    print(f"\n{DIM}model={m['model']} llm_calls={m.get('llm_calls')} tokens_in={m['tokens_in']} (cache read {m.get('cache_read_tokens', 0)}) "
          f"tokens_out={m['tokens_out']} cost=${m['cost_usd']:.4f} latency={m['latency_ms']}ms{END}")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()          # ANTHROPIC_API_KEY / ITER_BUDGET_USD from .env, if present
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("msg_id")
    ap.add_argument("--arch", default="V0", choices=["V0", "V1", "V2"])
    ap.add_argument("--variant", default="b", choices=["a", "b"])
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--replay", help="a run directory, e.g. runs/V0-b/v0b-r1")
    ap.add_argument("--allow-before-labels", action="store_true")
    args = ap.parse_args(argv)

    if not args.fake and len(list(LABELS_DIR.glob("*.json"))) < 100 and not args.allow_before_labels:
        print("Fewer than 100 blind labels in eval/labels: showing the agent's triage now can anchor your labels.\n"
              "Use --fake for a scripted demo, or pass --allow-before-labels if you accept that.", file=sys.stderr)
        return 2

    msg = get_message(args.msg_id)
    hr(f"{msg['msg_id']} · {msg['channel']} · {msg['lang']} · from {msg['from']}")
    for t in msg["thread"]:
        print(f"{DIM}{t['ts']} {t['role']}{END}\n" + textwrap.indent(textwrap.fill(t["text"], W - 4), "    "))

    if args.replay:
        run = Path(args.replay)
        trace = json.loads((run / "traces" / f"{args.msg_id}.json").read_text())
        out = json.loads((run / "outputs" / f"{args.msg_id}.json").read_text())
    else:
        if args.fake:
            from agents.fake_scripts import smoke_script
            llm = FakeLLM(smoke_script)
        elif has_api_key():
            llm = LLM()
        else:
            print("ANTHROPIC_API_KEY is not set; use --fake or --replay.", file=sys.stderr)
            return 2
        import run as run_mod
        arch_dir = args.arch + (f"-{args.variant}" if args.arch == "V0" else "")
        run_id = time.strftime("%Y%m%d-%H%M%S")
        rd = RunDir.open(arch_dir, run_id, {"arch": arch_dir, "run_id": run_id, "model": llm.model, "variant": args.variant},
                         root=RUNS_DIR / "_demo")
        meter, sb = Meter(), Sandbox(log_path=rd.log_path(args.msg_id))
        out, trace = run_mod.build_runner(args.arch, args.variant)(msg, llm, sb, meter, run_id)
        rd.write_trace(args.msg_id, trace)
        rd.write_output(args.msg_id, out)
    hr(f"trace · {out['meta']['arch']}")
    print_entries(trace)
    print_output(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
