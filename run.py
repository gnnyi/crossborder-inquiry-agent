"""Single entry point for every architecture.

    python run.py --arch V0 --run-id 2026-10-28-a                       # V0, variant b (route derived by code)
    python run.py --arch V0 --variant a --run-id ...                    # V0-a: model outputs route
    python run.py --arch V1 --run-id ... --limit 20 --concurrency 4
    python run.py --arch V0 --run-id ... --fault 7,0.1,0.05             # seed,timeout_rate,not_found_rate
    python run.py --arch V0 --run-id ... --model claude-haiku-5-5
    python run.py --arch V0 --run-id smoke --fake                       # scripted model, no API calls

Re-running the same --run-id resumes: messages with an output or an error file are skipped.
Runs land in runs/<ARCH>[-variant]/<run_id>/ (see runners/README.md section 4) and are never overwritten.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from agents.common import RUNS_DIR, RunDir, check_output, load_messages
from agents.llm import Budget, BudgetExceeded, FakeLLM, LLM, Meter, has_api_key, load_models
from sandbox.tools import FaultConfig, Sandbox

MESSAGE_TIMEOUT_S = 300


def build_runner(arch: str, variant: str):
    if arch == "V0":
        from agents.v0 import run_v0
        return lambda msg, llm, sb, meter, run_id: run_v0(msg, llm, sb, meter, variant=variant, run_id=run_id)
    if arch == "V1":
        from agents.v1 import run_v1
        return lambda msg, llm, sb, meter, run_id: run_v1(msg, llm, sb, meter, run_id=run_id)
    if arch == "V2":
        from agents.v2 import run_v2
        return lambda msg, llm, sb, meter, run_id: run_v2(msg, llm, sb, meter, run_id=run_id)
    raise SystemExit(f"unknown arch {arch}")


def parse_fault(s: str | None) -> FaultConfig | None:
    if not s:
        return None
    seed, t, nf = s.split(",")
    return FaultConfig(seed=int(seed), timeout_rate=float(t), not_found_rate=float(nf))


def process(msg: dict, rd: RunDir, runner, llm, fault: FaultConfig | None, run_id: str) -> str:
    mid = msg["msg_id"]
    meter = Meter()
    sb = Sandbox(log_path=rd.log_path(mid), fault=fault)
    t0 = time.monotonic()
    try:
        out, trace = runner(msg, llm, sb, meter, run_id)
    except BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001 — any runner crash becomes errors/<msg_id>.json, never a partial output
        rd.write_error(mid, "exception", f"{type(e).__name__}: {e}", latency_ms=round((time.monotonic() - t0) * 1000),
                       cost_usd=round(meter.cost_usd, 6), traceback=traceback.format_exc(limit=5))
        return "error"
    elapsed = time.monotonic() - t0
    if elapsed > MESSAGE_TIMEOUT_S:
        rd.write_error(mid, "timeout", f"took {elapsed:.0f}s > {MESSAGE_TIMEOUT_S}s", latency_ms=round(elapsed * 1000),
                       cost_usd=round(meter.cost_usd, 6))
        return "error"
    if isinstance(out.get("meta"), dict):
        out["meta"]["latency_ms"] = round(elapsed * 1000)
        out["meta"]["tool_latency_ms_simulated"] = sum(r["simulated_latency_ms"] for r in sb.log)
    rd.write_trace(mid, trace)
    problems = check_output(out, sb.log)
    if problems:   # last line of defence: never write an output the scorer would reject
        rd.write_error(mid, "exception", "contract: " + "; ".join(problems[:5]), latency_ms=round(elapsed * 1000),
                       cost_usd=round(meter.cost_usd, 6))
        return "error"
    rd.write_output(mid, out)
    return "fallback" if out["meta"].get("fallback") else "ok"


def main(argv: list[str] | None = None) -> int:
    load_dotenv()          # ANTHROPIC_API_KEY / ITER_BUDGET_USD from .env, if present
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arch", required=True, choices=["V0", "V1", "V2"])
    ap.add_argument("--variant", default="b", choices=["a", "b"], help="V0 only: a = model outputs route, b = code derives it")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--model", help="override config/models.yaml main")
    ap.add_argument("--fault", help="seed,timeout_rate,not_found_rate e.g. 7,0.1,0.05")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="comma-separated msg_ids")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--fake", action="store_true", help="scripted model, no API calls (pipeline smoke test)")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    args = ap.parse_args(argv)

    if args.fake:
        from agents.fake_scripts import smoke_script
        llm = FakeLLM(smoke_script)
    else:
        if not has_api_key():
            print("ANTHROPIC_API_KEY is not set. Use --fake for a scripted smoke run, or export the key.", file=sys.stderr)
            return 2
        llm = LLM(model=args.model, budget=Budget())
        llm.budget.check()

    arch_dir = args.arch + (f"-{args.variant}" if args.arch == "V0" else "")
    fault = parse_fault(args.fault)
    meta = {"arch": arch_dir, "run_id": args.run_id, "model": llm.model, "variant": args.variant if args.arch == "V0" else None,
            "models_config": load_models(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if fault:
        meta["fault"] = fault.to_dict()
    rd = RunDir.open(arch_dir, args.run_id, meta, root=Path(args.runs_dir))

    msgs = load_messages()
    if args.only:
        keep = set(args.only.split(","))
        msgs = [m for m in msgs if m["msg_id"] in keep]
    if args.limit:
        msgs = msgs[: args.limit]
    todo = [m for m in msgs if not rd.done(m["msg_id"])]
    print(f"{rd.path}: {len(msgs) - len(todo)} done, {len(todo)} to run")

    runner = build_runner(args.arch, args.variant)
    counts: dict[str, int] = {}
    stopped = False
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = {pool.submit(process, m, rd, runner, llm, fault, args.run_id): m["msg_id"] for m in todo}
        for f in as_completed(futures):
            try:
                status = f.result()
            except BudgetExceeded as e:
                if not stopped:
                    print(f"BUDGET STOP: {e}. Remaining messages are left unrun; resume later with the same --run-id.", file=sys.stderr)
                    stopped = True
                    for other in futures:
                        other.cancel()
                continue
            counts[status] = counts.get(status, 0) + 1
    spent = f", budget spent so far ${llm.budget.spent:.2f} of ${llm.budget.cap:.2f}" if not args.fake else ""
    print(f"finished: {counts}{spent}")
    return 3 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
