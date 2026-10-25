"""Shared loaders for the policy tables (eval/policy/*.yaml)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = REPO_ROOT / "eval" / "policy"


@lru_cache(maxsize=None)
def risk_policy(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or POLICY_DIR / "risk_policy.yaml").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def intents_table(path: str | None = None) -> dict:
    return yaml.safe_load(Path(path or POLICY_DIR / "intents.yaml").read_text(encoding="utf-8"))


def risk_flags() -> list[str]:
    return list(risk_policy()["flags"])


def route_levels() -> list[str]:
    return list(risk_policy()["route_levels"])


def intent_codes() -> list[str]:
    return [i["code"] for i in intents_table()["intents"]]


PRIORITIES = ["P0", "P1", "P2"]
TOOL_NAMES = ["get_order", "find_orders_by_email", "get_tracking", "get_product"]
