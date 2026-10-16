"""Общий контекст: конфиг, состояние, HTTP-, Claude- и Gemini-клиенты, часы."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from .config import Config
from .state import State

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def make_http(timeout: float = 15, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": BROWSER_UA, "Accept-Language": "ru,en;q=0.8"},
        transport=transport,
    )


def make_anthropic(api_key: str):
    if not api_key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=api_key, timeout=300.0, max_retries=2)


@dataclass
class Ctx:
    cfg: Config
    state: State
    http: httpx.Client
    llm: Any = None
    gemini: Any = None
    telegram: Any = None
    now: Callable[[], datetime] = utcnow
    dry_run: bool = False
    # Сводки и проверки срочного не идут параллельно
    heavy_lock: threading.Lock = field(default_factory=threading.Lock)
    imap_factory: Any = None
