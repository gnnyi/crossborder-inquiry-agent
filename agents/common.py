"""Shared plumbing for V0 / V1 / V2: message rendering, the submit_response tool (generated from the
output contract), contract assembly + validation, and the run directory (runners/README.md section 4).
"""
from __future__ import annotations

import copy
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from contract.validate import validate_output
from scoring.route import derive_route

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_PATH = REPO_ROOT / "eval" / "messages.jsonl"
CONTRACT_PATH = REPO_ROOT / "contract" / "output.schema.json"
RUNS_DIR = REPO_ROOT / "runs"
AS_OF = json.loads((REPO_ROOT / "eval" / "env" / "meta.json").read_text())["as_of"]


# ------------------------------------------------------------------ messages
def load_messages(path: Path = MESSAGES_PATH) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def get_message(msg_id: str) -> dict:
    for m in load_messages():
        if m["msg_id"] == msg_id:
            return m
    raise KeyError(msg_id)


def render_message(msg: dict, as_of: str = AS_OF) -> str:
    """The whole thread, oldest first, with who/when on every turn."""
    head = [f"Today is {as_of} (use this date for every day count).",
            f"Channel: {msg['channel']}", f"From: {msg['from']}"]
    if msg.get("subject"):
        head.append(f"Subject: {msg['subject']}")
    turns = []
    for i, t in enumerate(msg["thread"], 1):
        who = "CUSTOMER" if t["role"] == "customer" else "OUR AGENT (earlier reply)"
        turns.append(f"--- turn {i} · {who} · {t['ts']} ---\n{t['text']}")
    return "\n".join(head) + "\n\n<conversation>\n" + "\n\n".join(turns) + "\n</conversation>"


# ------------------------------------------------------------------ submit_response tool
CODE_FILLED = ("msg_id", "tool_calls", "meta")       # the agent never writes these; code fills them from facts


def submit_tool(variant: str = "b") -> dict:
    """submit_response, generated from contract/output.schema.json.

    The model fills only what it judges; code fills msg_id, tool_calls (from the sandbox log) and meta.
    Variant "b" also drops `route`: code derives it from risk_flags with scoring/route.py.
    """
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    drop = set(CODE_FILLED) | ({"route"} if variant == "b" else set())
    props = {k: _inline_refs(v, contract["$defs"]) for k, v in contract["properties"].items() if k not in drop}
    schema = {"type": "object", "additionalProperties": False, "properties": props,
              "required": [k for k in contract["required"] if k not in drop]}
    return {
        "name": "submit_response",
        "description": "Submit the final triage and reply for this conversation. Call exactly once, at the end.",
        "input_schema": schema,
    }


def _inline_refs(node: object, defs: dict) -> object:
    """Replace {"$ref": "#/$defs/x"} with the definition, so the tool schema is self-contained."""
    if isinstance(node, dict):
        if set(node) == {"$ref"}:
            return _inline_refs(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
        return {k: _inline_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return copy.deepcopy(node)


def assemble_output(msg_id: str, submitted: dict, tool_log: list[dict], meta: dict, variant: str) -> dict:
    out = {k: submitted.get(k) for k in ("intents", "priority", "risk_flags", "reply", "handoff_note")}
    out["risk_flags"] = out["risk_flags"] if out["risk_flags"] is not None else []
    if variant == "b":
        try:
            out["route"] = derive_route(out["risk_flags"])
        except ValueError:
            out["route"] = None          # unknown flag: leave it to the validator to report
    else:
        out["route"] = submitted.get("route")
    out["msg_id"] = msg_id
    out["tool_calls"] = [{"call_id": r["call_id"], "name": r["tool"], "args": r["args"], "status": r["status"]}
                         for r in tool_log if r["tool"] in ("get_order", "find_orders_by_email", "get_tracking", "get_product")
                         and isinstance(r["args"], dict)]
    out["meta"] = meta
    ordered = ["msg_id", "intents", "priority", "risk_flags", "route", "tool_calls", "reply", "handoff_note", "meta"]
    return {k: out[k] for k in ordered}


def check_output(out: dict, tool_log: list[dict]) -> list[str]:
    log_view = [r for r in tool_log if isinstance(r["args"], dict) and r["tool"] in
                ("get_order", "find_orders_by_email", "get_tracking", "get_product")]
    return validate_output(out, log_view, expected_msg_id=out.get("msg_id"))


def handoff_output(msg_id: str, reason: str, tool_log: list[dict], meta: dict) -> dict:
    """What the system emits when the agent could not finish: route to a human with the reason.
    intents=[OTHER] is a placeholder the contract requires; meta.fallback marks it."""
    meta = {**meta, "fallback": True, "handoff_reason": reason}
    sub = {"intents": ["OTHER"], "priority": "P1", "risk_flags": [], "reply": None,
           "handoff_note": f"Agent could not finish ({reason}); please handle manually."}
    out = assemble_output(msg_id, sub, tool_log, meta, variant="a")
    out["route"] = "ESCALATE"
    return out


def tool_result_payload(r: dict) -> str:
    """What the model sees after a sandbox call: everything except the tool name it already knows."""
    return json.dumps({k: r[k] for k in ("call_id", "status", "result", "error")}, ensure_ascii=False)


# ------------------------------------------------------------------ run directory
@dataclass
class RunDir:
    """runs/<arch>/<run_id>/ — never overwritten. Re-running the same run_id resumes it."""
    path: Path

    @classmethod
    def open(cls, arch: str, run_id: str, meta: dict, root: Path = RUNS_DIR) -> "RunDir":
        path = root / arch / run_id
        meta_path = path / "run_meta.json"
        if meta_path.exists():
            old = json.loads(meta_path.read_text())
            keys = ("arch", "model", "variant", "fault", "prompt_sha")   # a changed prompt means a different experiment
            if any(old.get(k) != meta.get(k) for k in keys):
                raise SystemExit(f"{path} exists with a different config ({ {k: old.get(k) for k in keys} }); use a new --run-id")
        else:
            for sub in ("outputs", "tool_logs", "errors", "traces"):
                (path / sub).mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        return cls(path)

    def done(self, msg_id: str) -> bool:
        return (self.path / "outputs" / f"{msg_id}.json").exists() or (self.path / "errors" / f"{msg_id}.json").exists()

    def log_path(self, msg_id: str) -> Path:
        """Tool log for a fresh attempt. A partial log from a killed attempt is moved aside, never appended to."""
        p = self.path / "tool_logs" / f"{msg_id}.jsonl"
        if p.exists():
            aside = self.path / "traces" / "aborted"
            aside.mkdir(parents=True, exist_ok=True)
            shutil.move(p, aside / f"{msg_id}.{int(time.time())}.jsonl")
        return p

    def write_output(self, msg_id: str, out: dict) -> None:
        _write_json(self.path / "outputs" / f"{msg_id}.json", out)

    def write_error(self, msg_id: str, error_type: str, message: str, **extra) -> None:
        _write_json(self.path / "errors" / f"{msg_id}.json", {"msg_id": msg_id, "error_type": error_type, "message": message, **extra})

    def write_trace(self, msg_id: str, trace: list[dict]) -> None:
        _write_json(self.path / "traces" / f"{msg_id}.json", trace)


def prompt_sha() -> str:
    """Fingerprint of the prompt text, stored in run_meta so a run is never resumed under a different prompt."""
    import hashlib
    return hashlib.sha256((REPO_ROOT / "agents" / "prompts.py").read_bytes()).hexdigest()[:12]


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    tmp.replace(path)
