"""Deterministic mock business API shared by every architecture (V0 / V1 / V2).

    sb = Sandbox(log_path="runs/V0/r1/tool_logs/m001.jsonl")      # faults off by default
    r = sb.get_order("10231")      # {"call_id": "c1", "tool": "get_order", "status": "ok", "result": {...}, "error": None}
    r = sb.call("get_tracking", {"order_id": "10231"})              # same thing, dispatched by name

Rules:
- Results are deep copies with stable ordering: the same input always returns the same result.
- Arguments are NOT repaired. "#10231", "NW-10231", 10231 (int) or " 10231" all return invalid_argument;
  normalising what the customer wrote is the agent's job.
- Every call appends one JSON line to the log: call_id, tool, args, status, result, simulated_latency_ms
  (plus error and fault_injected, which only the scorer reads).
- Fault injection is decided by sha256(seed, tool, canonical args, n-th identical call), so a run is
  reproducible while a retry of the same call can still succeed.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_DIR = REPO_ROOT / "eval" / "env"
SCHEMA_PATH = Path(__file__).resolve().parent / "tool_schemas.json"

STATUSES = ("ok", "not_found", "invalid_argument", "timeout")
TOOL_NAMES = ("get_order", "find_orders_by_email", "get_tracking", "get_product")
ORDER_ID_RE = re.compile(r"[0-9]+")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SKU_RE = re.compile(r"[A-Z]{3}-[0-9]{3}")
BASE_LATENCY_MS = {"get_order": 120, "find_orders_by_email": 180, "get_tracking": 220, "get_product": 90}


@dataclass(frozen=True)
class FaultConfig:
    seed: int = 0
    timeout_rate: float = 0.0
    not_found_rate: float = 0.0
    timeout_ms: int = 3000

    def __post_init__(self) -> None:
        if not (0 <= self.timeout_rate <= 1 and 0 <= self.not_found_rate <= 1 and self.timeout_rate + self.not_found_rate <= 1):
            raise ValueError("rates must be in [0, 1] and sum to at most 1")

    @property
    def enabled(self) -> bool:
        return self.timeout_rate > 0 or self.not_found_rate > 0

    def to_dict(self) -> dict:
        return {"seed": self.seed, "timeout_rate": self.timeout_rate, "not_found_rate": self.not_found_rate, "timeout_ms": self.timeout_ms}


def canonical_args(args: Any) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def unit_hash(*parts: Any) -> float:
    """Deterministic float in [0, 1) from the given parts (Python's hash() is salted per process)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / 2**64


class _Env:
    _cache: dict[str, "_Env"] = {}

    def __init__(self, env_dir: Path) -> None:
        self.meta = json.loads((env_dir / "meta.json").read_text(encoding="utf-8"))
        orders = json.loads((env_dir / "orders.json").read_text(encoding="utf-8"))
        products = json.loads((env_dir / "products.json").read_text(encoding="utf-8"))
        self.tracking = json.loads((env_dir / "tracking.json").read_text(encoding="utf-8"))
        self.orders = {o["order_id"]: o for o in orders}
        self.products = {p["sku"]: p for p in products}
        self.by_email: dict[str, list[dict]] = {}
        for o in sorted(orders, key=lambda o: (o["created_at"], o["order_id"])):
            self.by_email.setdefault(o["email"].lower(), []).append(o)

    @classmethod
    def load(cls, env_dir: Path) -> "_Env":
        key = str(env_dir.resolve())
        if key not in cls._cache:
            cls._cache[key] = cls(env_dir)
        return cls._cache[key]


class Sandbox:
    """One instance per message (call ids restart at c1). Not thread-safe."""

    def __init__(self, log_path: str | Path | None = None, fault: FaultConfig | None = None,
                 env_dir: str | Path | None = None, call_id_prefix: str = "c") -> None:
        self.env = _Env.load(Path(env_dir) if env_dir else ENV_DIR)
        self.fault = fault or FaultConfig()
        self.log_path = Path(log_path) if log_path else None
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.prefix = call_id_prefix
        self.log: list[dict] = []
        self._seen: Counter[tuple[str, str]] = Counter()

    @property
    def as_of(self) -> str:
        return self.env.meta["as_of"]

    # ------------------------------------------------------------ public tools
    def get_order(self, order_id: Any) -> dict:
        return self._run("get_order", {"order_id": order_id})

    def find_orders_by_email(self, email: Any) -> dict:
        return self._run("find_orders_by_email", {"email": email})

    def get_tracking(self, order_id: Any) -> dict:
        return self._run("get_tracking", {"order_id": order_id})

    def get_product(self, sku: Any) -> dict:
        return self._run("get_product", {"sku": sku})

    def call(self, name: str, args: dict | None) -> dict:
        """Dispatch a function-calling request. Unknown tools and bad argument shapes return invalid_argument."""
        return self._run(name, args)

    # ------------------------------------------------------------ internals
    def _run(self, tool: str, args: Any) -> dict:
        args_for_log = copy.deepcopy(args) if isinstance(args, dict) else args
        canon = canonical_args(args_for_log)
        key = (tool, canon)
        self._seen[key] += 1
        nth = self._seen[key]
        call_id = f"{self.prefix}{len(self.log) + 1}"

        status, result, error = self._validate(tool, args)
        injected = False
        if status is None:
            u = unit_hash(self.fault.seed, tool, canon, nth, "fault")
            if self.fault.enabled and u < self.fault.timeout_rate:
                status, result, error, injected = "timeout", None, "upstream timeout", True
            elif self.fault.enabled and u < self.fault.timeout_rate + self.fault.not_found_rate:
                status, result, error, injected = "not_found", None, "not found", True
            else:
                status, result, error = self._lookup(tool, args)
        if status == "timeout":
            latency = self.fault.timeout_ms
        else:
            latency = BASE_LATENCY_MS.get(tool, 100) + int(unit_hash(self.fault.seed, tool, canon, nth, "latency") * 80)

        record = {
            "call_id": call_id, "tool": tool, "args": args_for_log, "status": status,
            "result": copy.deepcopy(result), "error": error,
            "simulated_latency_ms": latency, "fault_injected": injected,
        }
        self.log.append(record)
        if self.log_path:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return {"call_id": call_id, "tool": tool, "status": status, "result": copy.deepcopy(result), "error": error}

    @staticmethod
    def _validate(tool: str, args: Any) -> tuple[str | None, None, str | None]:
        expected = {"get_order": "order_id", "get_tracking": "order_id", "find_orders_by_email": "email", "get_product": "sku"}
        if tool not in expected:
            return "invalid_argument", None, f"unknown tool {tool!r}"
        field = expected[tool]
        if not isinstance(args, dict) or set(args) != {field}:
            return "invalid_argument", None, f"{tool} takes exactly one argument: {field}"
        value = args[field]
        if not isinstance(value, str):
            return "invalid_argument", None, f"{field} must be a string"
        pattern = {"order_id": ORDER_ID_RE, "email": EMAIL_RE, "sku": SKU_RE}[field]
        if not pattern.fullmatch(value):
            hint = {"order_id": "digits only", "email": "an email address", "sku": "format AAA-000"}[field]
            return "invalid_argument", None, f"{field} must be {hint}"
        return None, None, None

    def _lookup(self, tool: str, args: dict) -> tuple[str, Any, str | None]:
        env = self.env
        if tool == "get_order":
            order = env.orders.get(args["order_id"])
            return ("ok", copy.deepcopy(order), None) if order else ("not_found", None, "order not found")
        if tool == "find_orders_by_email":
            email = args["email"].lower()
            orders = env.by_email.get(email, [])
            if not orders:
                return "not_found", None, "no orders for this email"
            summary = [{"order_id": o["order_id"], "created_at": o["created_at"], "status": o["status"],
                        "total": o["total"], "currency": o["currency"], "item_count": sum(i["qty"] for i in o["items"])}
                       for o in orders]
            return "ok", {"email": email, "orders": summary}, None
        if tool == "get_tracking":
            order = env.orders.get(args["order_id"])
            if not order:
                return "not_found", None, "order not found"
            shipments = []
            for s in order.get("shipments", []):
                t = env.tracking[s["tracking_number"]]
                shipments.append({"shipment_id": s["shipment_id"], "carrier": t["carrier"], "tracking_number": t["tracking_number"],
                                  "status": t["status"], "line_indexes": list(s["line_indexes"]),
                                  "events": sorted(copy.deepcopy(t["events"]), key=lambda e: e["ts"])})
            return "ok", {"order_id": order["order_id"], "order_status": order["status"], "shipments": shipments}, None
        if tool == "get_product":
            product = env.products.get(args["sku"])
            return ("ok", copy.deepcopy(product), None) if product else ("not_found", None, "product not found")
        raise AssertionError(tool)


def load_tool_schemas() -> list[dict]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["tools"]


def read_log(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
