"""Учёт расходов на Claude API в data/usage.jsonl."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_lock = threading.Lock()


@dataclass
class MonthUsage:
    usd: float = 0.0
    calls: int = 0
    searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _get(obj, name, default=0):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default) or default
    return getattr(obj, name, default) or default


def cost_usd(pricing: dict, model: str, usage) -> tuple[float, dict]:
    """Примерная стоимость вызова по ценам из config.pricing."""
    price = pricing.get(model) or {}
    pin, pout = float(price.get("input", 0)), float(price.get("output", 0))
    inp = int(_get(usage, "input_tokens"))
    out = int(_get(usage, "output_tokens"))
    cw = int(_get(usage, "cache_creation_input_tokens"))
    cr = int(_get(usage, "cache_read_input_tokens"))
    searches = int(_get(_get(usage, "server_tool_use", None), "web_search_requests"))
    usd = (inp * pin + cw * pin * 1.25 + cr * pin * 0.1 + out * pout) / 1_000_000
    usd += searches * float(pricing.get("web_search_per_1000", 10)) / 1000
    return usd, {"input_tokens": inp, "output_tokens": out, "cache_write": cw,
                 "cache_read": cr, "web_search_requests": searches}


def record(path: Path, now: datetime, purpose: str, model: str, usage, pricing: dict) -> float:
    usd, parts = cost_usd(pricing, model, usage)
    line = {"ts": now.isoformat(), "purpose": purpose, "model": model, **parts, "usd": round(usd, 6)}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return usd


def month_usage(path: Path, now: datetime) -> MonthUsage:
    m = MonthUsage()
    if not path.exists():
        return m
    prefix = now.strftime("%Y-%m")
    with _lock, path.open(encoding="utf-8") as fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not str(row.get("ts", "")).startswith(prefix):
                continue
            m.usd += float(row.get("usd", 0))
            m.calls += 1
            m.searches += int(row.get("web_search_requests", 0))
            m.input_tokens += int(row.get("input_tokens", 0))
            m.output_tokens += int(row.get("output_tokens", 0))
    return m


def budget_mode(cfg, now: datetime) -> str:
    """ok | no_search | stopped — по расходу за текущий месяц."""
    limit = float(cfg.get("budget.monthly_usd", 0) or 0)
    if limit <= 0:
        return "ok"
    spent = month_usage(cfg.data_dir / "usage.jsonl", now).usd
    if spent >= limit * float(cfg.get("budget.stop_claude_at", 1.0)):
        return "stopped"
    if spent >= limit * float(cfg.get("budget.disable_search_at", 0.8)):
        return "no_search"
    return "ok"
