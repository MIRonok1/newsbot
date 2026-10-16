"""RSS-ленты: параллельная загрузка, окно времени, очистка, дедупликация."""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import httpx

from .config import Feed
from .textutil import html_to_text, truncate

log = logging.getLogger(__name__)

_GN_TAIL = re.compile(r"\s+[-–—|]\s+([^-–—|]{2,80})$")
_PUNCT = re.compile(r"[^\w\s]", re.U)


@dataclass
class NewsItem:
    id: str
    source: str
    topic: str
    title: str
    summary: str
    link: str
    published: datetime | None
    priority: str = "normal"
    ref: str = ""  # номер во входе модели: n1, n2, ...


def item_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).encode("utf-8")).hexdigest()[:16]


def normalize_title(title: str) -> str:
    t = _GN_TAIL.sub("", title.strip())
    t = t.lower().replace("ё", "е")
    t = _PUNCT.sub(" ", t)
    return " ".join(t.split())


def _entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def parse_feed(content: bytes, feed: Feed, cutoff: datetime | None, desc_chars: int = 280,
               default_max: int = 25) -> list[NewsItem]:
    parsed = feedparser.parse(content)
    if not parsed.entries and (parsed.bozo or not parsed.get("version")):
        raise ValueError("ответ не похож на RSS/Atom")
    items: list[NewsItem] = []
    for e in parsed.entries:
        title = html_to_text(e.get("title", ""))
        if not title:
            continue
        published = _entry_time(e)
        if cutoff is not None and (published is None or published < cutoff):
            continue
        source = feed.name
        if feed.google_news:
            src = (e.get("source") or {}).get("title")
            m = _GN_TAIL.search(title)
            if m:
                source = src or m.group(1).strip()
                title = title[: m.start()].strip()
            elif src:
                source = src
        summary = html_to_text(e.get("summary", "") or e.get("description", ""))
        if feed.google_news or normalize_title(summary).startswith(normalize_title(title)):
            summary = ""  # у Google News описание дублирует заголовок
        link = e.get("link", "")
        items.append(NewsItem(
            id=item_id(link, title), source=source, topic=feed.topic, title=title,
            summary=truncate(summary, desc_chars), link=link, published=published,
            priority=feed.priority,
        ))
    items.sort(key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items[: feed.max_items or default_max]


def fetch_feed(http: httpx.Client, feed: Feed, cutoff: datetime | None, desc_chars: int,
               default_max: int, timeout: float) -> list[NewsItem]:
    resp = http.get(feed.url, timeout=timeout)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    return parse_feed(resp.content, feed, cutoff, desc_chars, default_max)


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """Убирает дубли по нормализованному заголовку. Приоритетные ленты выигрывают."""
    ordered = sorted(items, key=lambda i: (i.priority != "high",
                                           -(i.published.timestamp() if i.published else 0)))
    seen: set[str] = set()
    out = []
    for it in ordered:
        key = normalize_title(it.title)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def collect_news(ctx, window_hours: float | None, feeds: list[Feed] | None = None
                 ) -> tuple[list[NewsItem], list[tuple[str, str]]]:
    """Грузит все ленты параллельно. Возвращает (новости, [(лента, ошибка)])."""
    cfg = ctx.cfg
    feeds = feeds if feeds is not None else cfg.feeds
    now = ctx.now()
    desc = cfg.get("digest.description_chars", 280)
    default_max = cfg.get("digest.max_items_per_feed", 25)
    timeout = cfg.get("http.timeout", 15)

    def one(feed: Feed):
        cutoff = None
        if window_hours is not None:
            hours = max(window_hours, feed.window_hours or 0)
            cutoff = now - timedelta(hours=hours)
        return fetch_feed(ctx.http, feed, cutoff, desc, default_max, timeout)

    items: list[NewsItem] = []
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=cfg.get("http.workers", 8)) as pool:
        futures = {feed.name: pool.submit(one, feed) for feed in feeds}
        for name, fut in futures.items():
            try:
                items.extend(fut.result())
            except Exception as e:  # одна упавшая лента не ломает сводку
                msg = _short_error(e)
                log.warning("лента %s: %s", name, msg)
                failed.append((name, msg))
    return dedupe(items), failed


def _short_error(e: Exception) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "таймаут"
    if isinstance(e, httpx.HTTPError):
        return f"сеть: {type(e).__name__}"
    return str(e) or type(e).__name__
