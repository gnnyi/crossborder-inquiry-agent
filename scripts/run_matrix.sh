#!/usr/bin/env bash
# Stage 5 run matrix, in budget-priority order. Needs the API key of the provider in config/models.yaml (in .env).
#   scripts/run_matrix.sh calibrate        # 10 messages of V0-b: read cost/msg before spending more
#   scripts/run_matrix.sh baseline         # V0-b full run r1 = start of the iteration loop (docs/iteration_log.md)
#   scripts/run_matrix.sh final TAG        # after iterating: the comparison matrix, run ids prefixed with TAG
# run.py stops by itself at 85% of ITER_BUDGET_USD (default $30) and exits 3; this script stops with it.
# Re-running the same command resumes unfinished runs.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
FAULT=7,0.1,0.05

run() {            # run <arch> <run_id> [extra args...]
  local arch=$1 id=$2; shift 2
  $PY run.py --arch "$arch" --run-id "$id" --concurrency 4 "$@" || { rc=$?; [ $rc -eq 3 ] && { echo "budget stop"; exit 3; }; exit $rc; }
}

case "${1:-}" in
  calibrate)
    run V0 calib-10 --variant b --limit 10
    $PY scoring/label_free.py runs/V0-b/calib-10 | grep -E "成本|缓存|步数" ;;
  baseline)
    run V0 v0b-r1 --variant b
    $PY scoring/label_free.py runs/V0-b/v0b-r1 --failures --out reports/v0b-r1.md
    echo "next: read runs/V0-b/v0b-r1/label_free_failures.json and the traces, then log round 1 in docs/iteration_log.md" ;;
  final)
    T=${2:?usage: final TAG}
    # priority 1: the two configs the task protects (V0-b, V1) x3
    run V0 "$T-r1" --variant b; run V1 "$T-r1"
    run V0 "$T-r2" --variant b; run V1 "$T-r2"
    run V0 "$T-r3" --variant b; run V1 "$T-r3"
    # priority 2: the route ablation and V2
    run V0 "$T-r1" --variant a; run V2 "$T-r1"
    # priority 3: fault injection, one each
    run V0 "$T-fault7" --variant b --fault $FAULT; run V1 "$T-fault7" --fault $FAULT; run V2 "$T-fault7" --fault $FAULT
    # priority 5: remaining repeats
    run V0 "$T-r2" --variant a; run V0 "$T-r3" --variant a; run V2 "$T-r2"; run V2 "$T-r3"
    $PY scoring/label_free.py runs/V0-a/$T-r1 runs/V0-b/$T-r1 runs/V1/$T-r1 runs/V2/$T-r1 runs/V0-b/$T-fault7 runs/V1/$T-fault7 runs/V2/$T-fault7 --out reports/label_free_final.md
    echo "wrote reports/label_free_final.md" ;;
  *) sed -n 2,7p "$0"; exit 2 ;;
esac
