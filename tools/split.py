"""Stratified dev / test split of the labelled messages (run once, after labelling is finished).

    .venv/bin/python tools/split.py                  # dev 100 / test 50 when 150 are labelled
    .venv/bin/python tools/split.py --dev 70 --test 30
    .venv/bin/python tools/split.py --verify         # check the sealed test labels have not changed

Strata: long thread (>= 5 turns) x multi-intent (2 intents) x gold_route. Both splits are guaranteed to
contain long threads and multi-intent messages. Writes eval/splits.json and refuses to overwrite it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LABELS_DIR = REPO_ROOT / "eval" / "labels"
MESSAGES = REPO_ROOT / "eval" / "messages.jsonl"
SPLITS = REPO_ROOT / "eval" / "splits.json"
SEED = 20261101


def load(labels_dir: Path, messages_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    labels = {json.loads(f.read_text(encoding="utf-8"))["msg_id"]: json.loads(f.read_text(encoding="utf-8")) for f in sorted(labels_dir.glob("*.json"))}
    msgs = {m["msg_id"]: m for m in (json.loads(x) for x in messages_path.read_text(encoding="utf-8").splitlines() if x.strip())}
    return labels, msgs


def stratum(label: dict, msg: dict) -> tuple[bool, bool, str]:
    return (len(msg["thread"]) >= 5, len(label["intents"]) == 2, label["gold_route"])


def make_split(labels: dict[str, dict], msgs: dict[str, dict], n_test: int, seed: int = SEED) -> tuple[list[str], list[str]]:
    missing = sorted(set(labels) - set(msgs))
    if missing:
        raise SystemExit(f"labels without a message: {missing[:5]}")
    groups: dict[tuple, list[str]] = defaultdict(list)
    for mid in sorted(labels):
        groups[stratum(labels[mid], msgs[mid])].append(mid)
    rng = random.Random(seed)
    for k in sorted(groups):
        rng.shuffle(groups[k])
    total = len(labels)
    # largest-remainder allocation of the test quota across strata
    quotas = {k: len(v) * n_test / total for k, v in groups.items()}
    alloc = {k: int(q) for k, q in quotas.items()}
    for k in sorted(groups, key=lambda k: (-(quotas[k] - alloc[k]), str(k)))[: n_test - sum(alloc.values())]:
        alloc[k] += 1
    test = sorted(x for k, v in groups.items() for x in v[: alloc[k]])
    dev = sorted(set(labels) - set(test))

    def has(ids: list[str], idx: int) -> bool:
        return any(stratum(labels[i], msgs[i])[idx] for i in ids)

    for name, ids in [("dev", dev), ("test", test)]:
        for idx, what in [(0, "long-thread"), (1, "multi-intent")]:
            if not has(ids, idx):
                raise SystemExit(f"{name} split has no {what} messages — label more of them before splitting")
    return dev, test


def test_hash(labels_dir: Path, test: list[str]) -> str:
    h = hashlib.sha256()
    for mid in sorted(test):
        h.update(mid.encode())
        h.update((labels_dir / f"{mid}.json").read_bytes())
    return h.hexdigest()


def summary(ids: list[str], labels: dict, msgs: dict) -> str:
    n = len(ids)
    long_ = sum(1 for i in ids if len(msgs[i]["thread"]) >= 5)
    multi = sum(1 for i in ids if len(labels[i]["intents"]) == 2)
    routes = {r: sum(1 for i in ids if labels[i]["gold_route"] == r) for r in ["AUTO_REPLY", "DRAFT_FOR_REVIEW", "ESCALATE"]}
    return f"{n} msgs | long threads {long_} ({long_ / n:.0%}) | multi-intent {multi} ({multi / n:.0%}) | " + " / ".join(f"{k} {v}" for k, v in routes.items())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dev", type=int)
    ap.add_argument("--test", type=int)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--labels-dir", default=str(LABELS_DIR))
    ap.add_argument("--messages", default=str(MESSAGES))
    ap.add_argument("--out", default=str(SPLITS))
    ap.add_argument("--force", action="store_true", help="overwrite an existing split (breaks the test seal)")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args(argv)
    labels_dir, out = Path(args.labels_dir), Path(args.out)

    if args.verify:
        sp = json.loads(out.read_text(encoding="utf-8"))
        ok = test_hash(labels_dir, sp["test"]) == sp["test_sha256"]
        print("test labels unchanged since sealing" if ok else "WARNING: test labels changed since the split was sealed")
        return 0 if ok else 1
    if out.exists() and not args.force:
        print(f"{out} already exists — the test set is sealed. Use --verify, or --force to redo (this breaks the seal).", file=sys.stderr)
        return 2
    labels, msgs = load(labels_dir, Path(args.messages))
    total = len(labels)
    if args.dev is None and args.test is None:
        n_test = 50 if total == 150 else round(total / 3)
    elif args.test is None:
        n_test = total - args.dev
    else:
        n_test = args.test
    n_dev = total - n_test if args.dev is None else args.dev
    if n_dev + n_test != total or n_test <= 0 or n_dev <= 0:
        print(f"{total} labels cannot be split into dev {n_dev} + test {n_test}", file=sys.stderr)
        return 2
    dev, test = make_split(labels, msgs, n_test, args.seed)
    sp = {"version": 1, "seed": args.seed, "created_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "strata": "long_thread x multi_intent x gold_route", "dev": dev, "test": test, "test_sha256": test_hash(labels_dir, test)}
    out.write_text(json.dumps(sp, indent=2) + "\n", encoding="utf-8")
    print(f"dev : {summary(dev, labels, msgs)}")
    print(f"test: {summary(test, labels, msgs)}")
    print(f"\nwritten to {out}")
    print("\n现在封存 test：")
    print("  1. git add eval/splits.json eval/labels && git commit -m 'Seal test split'")
    print("  2. 之后调 prompt / 架构一律只用 --split dev；test 只在每个版本定稿时跑一次")
    print("  3. 不要再打开 test 消息或修改它们的金标；python tools/split.py --verify 可以检查是否被改过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
