"""Derive gold_route from risk_flags using eval/policy/risk_policy.yaml.

The strictest route among the flags wins; no flags means the policy's default_route (AUTO_REPLY).
"""
from __future__ import annotations

from collections.abc import Iterable

from scoring.policy import risk_policy


class UnknownFlag(ValueError):
    pass


def route_rank(route: str, policy: dict | None = None) -> int:
    levels = (policy or risk_policy())["route_levels"]
    if route not in levels:
        raise ValueError(f"unknown route {route!r}")
    return levels.index(route)


def derive_route(flags: Iterable[str], policy: dict | None = None) -> str:
    policy = policy or risk_policy()
    route = policy["default_route"]
    for flag in flags:
        spec = policy["flags"].get(flag)
        if spec is None:
            raise UnknownFlag(f"unknown risk flag {flag!r}")
        if route_rank(spec["min_route"], policy) > route_rank(route, policy):
            route = spec["min_route"]
    return route


def priority_floor(flags: Iterable[str], policy: dict | None = None) -> str | None:
    """'P0' when any flag forces P0 by policy, else None (P1/P2 need human judgement)."""
    policy = policy or risk_policy()
    p0 = set(policy["priority"]["p0_flags"])
    return "P0" if p0 & set(flags) else None


def is_auto(route: str | None) -> bool:
    return route == "AUTO_REPLY"
