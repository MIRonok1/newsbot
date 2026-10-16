"""Загрузка config.yaml и секретов из .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


GOOGLE_NEWS = {
    "ru": "https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru",
    "en": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
}


@dataclass
class Feed:
    name: str
    url: str
    topic: str
    google_news: bool = False
    max_items: int | None = None
    priority: str = "normal"
    window_hours: float | None = None


@dataclass
class Market:
    key: str
    name: str
    source: str
    secid: str | None = None
    code: str | None = None
    symbol: str | None = None
    alert_pct: float | None = None


@dataclass
class Secrets:
    anthropic_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    imap_host: str = ""
    imap_user: str = ""
    imap_password: str = ""
    ics_url: str = ""

    @property
    def mail_enabled(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)


@dataclass
class Config:
    raw: dict
    secrets: Secrets
    data_dir: Path
    feeds: list[Feed] = field(default_factory=list)
    markets: list[Market] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw.get("timezone", "Europe/Moscow"))

    @property
    def profile(self) -> str:
        return self.raw.get("profile", "").strip()

    def section(self, name: str) -> dict:
        return self.raw.get(name) or {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def rubrics(self) -> list[dict]:
        return self.get("digest.rubrics", [])


def _parse_feed(item: dict, idx: int) -> Feed:
    if not isinstance(item, dict):
        raise ConfigError(f"feeds[{idx}]: ожидается словарь")
    name = item.get("name")
    topic = item.get("topic")
    if not name or not topic:
        raise ConfigError(f"feeds[{idx}]: нужны поля name и topic")
    if item.get("google_news"):
        lang = item.get("lang", "ru")
        if lang not in GOOGLE_NEWS:
            raise ConfigError(f"лента «{name}»: lang должен быть ru или en")
        url = GOOGLE_NEWS[lang].format(q=quote_plus(item["google_news"]))
        gn = True
    elif item.get("url"):
        url = item["url"]
        gn = "news.google.com" in url
    else:
        raise ConfigError(f"лента «{name}»: нужен url или google_news")
    return Feed(
        name=name,
        url=url,
        topic=topic,
        google_news=gn,
        max_items=item.get("max_items"),
        priority=item.get("priority", "normal"),
        window_hours=item.get("window_hours"),
    )


def _parse_market(item: dict, idx: int) -> Market:
    try:
        m = Market(**item)
    except TypeError as e:
        raise ConfigError(f"markets[{idx}]: {e}") from None
    need = {"moex_index": "secid", "moex_currency": "secid", "cbr": "code", "yahoo": "symbol"}
    if m.source not in need:
        raise ConfigError(f"markets[{idx}] ({m.key}): неизвестный source {m.source!r}")
    if not getattr(m, need[m.source]):
        raise ConfigError(f"markets[{idx}] ({m.key}): для {m.source} нужно поле {need[m.source]}")
    return m


def build_config(raw: dict, secrets: Secrets | None = None, data_dir: Path | str = "data") -> Config:
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml пуст или повреждён")
    feeds = [_parse_feed(f, i) for i, f in enumerate(raw.get("feeds") or [])]
    markets = [_parse_market(m, i) for i, m in enumerate(raw.get("markets") or [])]
    rubric_keys = {r["key"] for r in raw.get("digest", {}).get("rubrics", [])}
    for f in feeds:
        if rubric_keys and f.topic not in rubric_keys:
            raise ConfigError(f"лента «{f.name}»: topic {f.topic!r} не совпадает ни с одной рубрикой")
    ZoneInfo(raw.get("timezone", "Europe/Moscow"))
    return Config(raw=raw, secrets=secrets or Secrets(), data_dir=Path(data_dir), feeds=feeds, markets=markets)


def load_config(path: str | Path = "config.yaml", env_file: str | Path | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"не найден {path}")
    env_path = Path(env_file) if env_file else path.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    secrets = Secrets(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_user=os.getenv("IMAP_USER", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        ics_url=os.getenv("ICS_URL", "").strip(),
    )
    data_dir = Path(os.getenv("DATA_DIR", "").strip() or path.parent / "data")
    return build_config(raw, secrets, data_dir)
