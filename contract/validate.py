"""Validate runner outputs (and gold labels) against the contract.

    python contract/validate.py runs/V0/<run_id>/outputs           # every *.json in the dir
    python contract/validate.py path/to/m001.json --log path/to/m001.jsonl
    python contract/validate.py eval/labels --labels

Schema checks come from output.schema.json; the extra semantic checks are things JSON Schema cannot
express (line ranges ordered, call ids unique and present in tool_calls, consistency with the tool log).
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

CONTRACT_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_errors(obj: object, name: str) -> list[str]:
    errs = sorted(_validator(name).iter_errors(obj), key=lambda e: list(e.absolute_path))
    return [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in errs]


def validate_output(obj: object, tool_log: list[dict] | None = None, expected_msg_id: str | None = None) -> list[str]:
    """Return a list of human-readable problems; empty list = valid."""
    errors = _schema_errors(obj, "output.schema.json")
    if errors or not isinstance(obj, dict):
        return errors
    if expected_msg_id and obj["msg_id"] != expected_msg_id:
        errors.append(f"msg_id {obj['msg_id']} != expected {expected_msg_id}")
    ids = [c["call_id"] for c in obj["tool_calls"]]
    if len(ids) != len(set(ids)):
        errors.append("tool_calls: duplicate call_id")
    for i, claim in enumerate((obj["reply"] or {}).get("claims", [])):
        src = claim["source"]
        if src["type"] == "kb" and src["lines"][0] > src["lines"][1]:
            errors.append(f"reply/claims/{i}: lines start > end")
        if src["type"] == "tool" and src["call_id"] not in ids:
            errors.append(f"reply/claims/{i}: call_id {src['call_id']} not in tool_calls")
    if tool_log is not None:
        logged = {r["call_id"]: r for r in tool_log}
        for c in obj["tool_calls"]:
            r = logged.get(c["call_id"])
            if r is None:
                errors.append(f"tool_calls: {c['call_id']} not in tool log")
            elif r["tool"] != c["name"] or r["args"] != c["args"] or r["status"] != c["status"]:
                errors.append(f"tool_calls: {c['call_id']} differs from tool log")
    return errors


def validate_label(obj: object) -> list[str]:
    errors = _schema_errors(obj, "label.schema.json")
    if errors or not isinstance(obj, dict):
        return errors
    for i, span in enumerate(obj["gold_kb_spans"]):
        if span["lines"][0] > span["lines"][1]:
            errors.append(f"gold_kb_spans/{i}: start > end")
    return errors


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="a .json file or a directory of them")
    ap.add_argument("--log", help="tool log (.jsonl) for a single output file")
    ap.add_argument("--labels", action="store_true", help="validate gold label files instead of outputs")
    args = ap.parse_args(argv)
    path = Path(args.path)
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    bad = 0
    for f in files:
        try:
            obj = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"{f}: invalid JSON: {e}")
            bad += 1
            continue
        if args.labels:
            errs = validate_label(obj)
        else:
            log = None
            if args.log:
                log = [json.loads(x) for x in Path(args.log).read_text(encoding="utf-8").splitlines() if x.strip()]
            errs = validate_output(obj, log, expected_msg_id=f.stem)
        if errs:
            bad += 1
            print(f"{f}:")
            for e in errs:
                print(f"  - {e}")
    print(f"{len(files) - bad}/{len(files)} valid")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
