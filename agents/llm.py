"""One place that talks to the Anthropic API.

    llm = LLM()                                           # provider + model from config/models.yaml (key from its key_env)
    llm = FakeLLM(script)                                 # replays scripted replies, no network
    reply = llm.create(system=..., messages=..., tools=..., meter=meter, tag="v0")

Every call is metered (tokens in/out, cache write/read, cost, latency) into the per-message Meter and
the process-wide Budget. The Budget refuses new calls once spend reaches 85% of the cap and raises
BudgetExceeded, so a run stops cleanly instead of overspending.

Content blocks are plain dicts ({"type": "text" | "tool_use" | "thinking", ...}) so the agent code,
the FakeLLM and the traces all use the same shape. Thinking blocks are passed back unchanged.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
LEDGER_PATH = REPO_ROOT / "runs" / "_budget_ledger.jsonl"
DEFAULT_BUDGET_USD = 30.0
STOP_FRACTION = 0.85


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    """Non-retryable API failure, or retries exhausted."""


class ProviderBlocked(RuntimeError):
    """401 / 402 / 403: bad key, no balance, no permission. Not the agent's fault: the whole run must stop
    and the message must not be recorded as a failure."""


def load_models(provider: str | None = None) -> dict:
    """The active provider's settings flattened: provider, base_url, key_env, main, cheap, effort, auto_cache, max_tokens.
    Provider comes from the argument, then $LLM_PROVIDER, then models.yaml."""
    raw = yaml.safe_load((CONFIG_DIR / "models.yaml").read_text(encoding="utf-8"))
    name = provider or os.environ.get("LLM_PROVIDER") or raw["provider"]
    if name not in raw["providers"]:
        raise KeyError(f"unknown provider {name!r}; config/models.yaml has {sorted(raw['providers'])}")
    return {"provider": name, "max_tokens": raw.get("max_tokens", 8000), **raw["providers"][name]}


def load_pricing() -> dict:
    return yaml.safe_load((CONFIG_DIR / "pricing.yaml").read_text(encoding="utf-8"))["models"]


def cost_usd(model: str, usage: dict, pricing: dict | None = None) -> float:
    pricing = pricing or load_pricing()
    p = pricing.get(model)
    if p is None:
        raise KeyError(f"no price for model {model!r} in config/pricing.yaml")
    return (usage["input_tokens"] * p["input"] + usage["output_tokens"] * p["output"]
            + usage["cache_write_tokens"] * p["cache_write"] + usage["cache_read_tokens"] * p["cache_read"]) / 1e6


# ------------------------------------------------------------------ metering
@dataclass
class Meter:
    """Per-message totals. One Meter per message; V2 shares it across orchestrator and workers."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    api_ms: float = 0.0
    calls: int = 0
    by_tag: dict = field(default_factory=dict)

    def add(self, tag: str, usage: dict, cost: float, ms: float) -> None:
        self.input_tokens += usage["input_tokens"]
        self.output_tokens += usage["output_tokens"]
        self.cache_write_tokens += usage["cache_write_tokens"]
        self.cache_read_tokens += usage["cache_read_tokens"]
        self.cost_usd += cost
        self.api_ms += ms
        self.calls += 1
        t = self.by_tag.setdefault(tag, {"calls": 0, "cost_usd": 0.0})
        t["calls"] += 1
        t["cost_usd"] += cost

    @property
    def tokens_in(self) -> int:
        """All prompt tokens, cached or not (what the contract's meta.tokens_in reports)."""
        return self.input_tokens + self.cache_write_tokens + self.cache_read_tokens


class Budget:
    """Process-wide spend counter, persisted to runs/_budget_ledger.jsonl so the cap spans runs."""

    def __init__(self, cap_usd: float | None = None, ledger: Path | None = LEDGER_PATH) -> None:
        env_cap = os.environ.get("ITER_BUDGET_USD")
        self.cap = cap_usd if cap_usd is not None else float(env_cap) if env_cap else DEFAULT_BUDGET_USD
        self.ledger = ledger
        self._lock = threading.Lock()
        self.spent = 0.0
        if ledger and ledger.exists():
            self.spent = sum(json.loads(line)["cost_usd"] for line in ledger.read_text().splitlines() if line.strip())

    @property
    def stop_at(self) -> float:
        return self.cap * STOP_FRACTION

    def check(self) -> None:
        if self.spent >= self.stop_at:
            raise BudgetExceeded(f"spent ${self.spent:.2f} >= {STOP_FRACTION:.0%} of ${self.cap:.2f} cap")

    def charge(self, cost: float, model: str, tag: str) -> None:
        with self._lock:
            self.spent += cost
            if self.ledger and cost > 0:
                self.ledger.parent.mkdir(parents=True, exist_ok=True)
                with self.ledger.open("a") as fh:
                    fh.write(json.dumps({"ts": time.time(), "model": model, "tag": tag, "cost_usd": cost}) + "\n")


# ------------------------------------------------------------------ replies
@dataclass
class Reply:
    content: list[dict]
    stop_reason: str
    usage: dict
    cost_usd: float = 0.0
    latency_ms: float = 0.0

    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b["type"] == "tool_use"]

    def text(self) -> str:
        return "\n".join(b["text"] for b in self.content if b["type"] == "text")


def _zero_usage() -> dict:
    return {"input_tokens": 0, "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0}


class _Base:
    model: str
    budget: Budget
    pricing: dict

    def _record(self, reply: Reply, meter: Meter | None, tag: str) -> Reply:
        reply.cost_usd = cost_usd(self.model, reply.usage, self.pricing)
        self.budget.charge(reply.cost_usd, self.model, tag)
        if meter is not None:
            meter.add(tag, reply.usage, reply.cost_usd, reply.latency_ms)
        return reply


# ------------------------------------------------------------------ real API
class LLM(_Base):
    RETRY_STATUSES = {429, 500, 502, 503, 504, 529}

    def __init__(self, model: str | None = None, budget: Budget | None = None, max_retries: int = 4,
                 base_delay: float = 2.0, effort: str | None = None, max_tokens: int | None = None,
                 sleep: Callable[[float], None] = time.sleep, client: Any = None, provider: str | None = None) -> None:
        cfg = load_models(provider)
        self.provider = cfg["provider"]
        self.model = model or cfg["main"]
        self.effort = effort or cfg.get("effort")
        self.auto_cache = cfg.get("auto_cache", True)
        self.max_tokens = max_tokens or cfg.get("max_tokens", 8000)
        self.budget = budget or Budget()
        self.pricing = load_pricing()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        if client is None:
            import anthropic
            # we own retries so they are counted and bounded; base_url is explicit so a stray ANTHROPIC_BASE_URL can't redirect us
            client = anthropic.Anthropic(api_key=os.environ.get(cfg["key_env"]), base_url=cfg.get("base_url") or "https://api.anthropic.com",
                                         max_retries=0)
        self.client = client

    def create(self, system: list[dict], messages: list[dict], tools: list[dict], meter: Meter | None = None,
               tag: str = "", max_tokens: int | None = None) -> Reply:
        import anthropic
        self.budget.check()
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens or self.max_tokens, system=system, messages=messages, tools=tools,
        )
        if self.auto_cache:
            kwargs["cache_control"] = {"type": "ephemeral"}   # auto-breakpoint on the last block: the growing loop history is cached too
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        for attempt in range(self.max_retries + 1):
            t0 = time.monotonic()
            try:
                resp = self.client.messages.create(**kwargs)
                break
            except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError) as e:
                err = e
            except anthropic.APIStatusError as e:
                if e.status_code in (401, 402, 403):
                    raise ProviderBlocked(f"{e.status_code}: {e.message}") from e
                if e.status_code not in self.RETRY_STATUSES:
                    raise LLMError(f"{e.status_code}: {e.message}") from e
                err = e
            if attempt == self.max_retries:
                raise LLMError(f"gave up after {self.max_retries} retries: {err}") from err
            self.sleep(min(self.base_delay * 2 ** attempt + random.uniform(0, 1), 60))
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_write_tokens": u.cache_creation_input_tokens or 0, "cache_read_tokens": u.cache_read_input_tokens or 0}
        content = [b.model_dump(exclude_none=True) for b in resp.content]
        reply = Reply(content=content, stop_reason=resp.stop_reason, usage=usage, latency_ms=(time.monotonic() - t0) * 1000)
        return self._record(reply, meter, tag)


# ------------------------------------------------------------------ fake
Script = Callable[[dict], Reply | dict] | list


class FakeLLM(_Base):
    """Replays scripted replies. `script` is either a list (consumed in order) or a function that gets the
    request {"system", "messages", "tools", "tag"} and returns a Reply (or a dict with "content")."""

    def __init__(self, script: Script, model: str = "fake", budget: Budget | None = None,
                 usage: dict | None = None) -> None:
        self.model = model
        self.script = list(script) if isinstance(script, list) else script
        self.budget = budget or Budget(cap_usd=float("inf"), ledger=None)
        self.pricing = load_pricing() if model == "fake" else {**load_pricing()}
        self.usage = usage or {"input_tokens": 100, "output_tokens": 50, "cache_write_tokens": 0, "cache_read_tokens": 0}
        self.requests: list[dict] = []

    def create(self, system: list[dict], messages: list[dict], tools: list[dict], meter: Meter | None = None,
               tag: str = "", max_tokens: int | None = None) -> Reply:
        self.budget.check()
        req = {"system": system, "messages": json.loads(json.dumps(messages)), "tools": tools, "tag": tag}
        self.requests.append(req)
        if isinstance(self.script, list):
            if not self.script:
                raise AssertionError("FakeLLM script exhausted")
            item = self.script.pop(0)
        else:
            item = self.script(req)
        if isinstance(item, Exception):
            raise item
        reply = item if isinstance(item, Reply) else Reply(content=item["content"], stop_reason=item.get("stop_reason", "end_turn"),
                                                           usage=dict(item.get("usage", self.usage)))
        return self._record(reply, meter, tag)


def tool_use(name: str, input: dict, id: str | None = None) -> dict:
    """Build a tool_use block (for scripts and tests)."""
    return {"type": "tool_use", "id": id or f"toolu_{name}_{random.randrange(10**8)}", "name": name, "input": input}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def has_api_key(provider: str | None = None) -> bool:
    return bool(os.environ.get(load_models(provider)["key_env"]))
