"""Срочные уведомления между сводками: рынки по порогам + Haiku-фильтр новостей и почты."""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import llm
from .feeds import NewsItem, collect_news
from .mail import MailItem, fetch_mail, mail_for_prompt
from .markets import Quote, fetch_quotes
from .prompts import ALERT_SCHEMA, ALERT_SYSTEM
from .textutil import fmt_num, fmt_pct

log = logging.getLogger(__name__)


@dataclass
class MarketAlert:
    key: str
    level: int
    text: str


def parse_alerts(text: str) -> list[dict]:
    """Устойчивый разбор {"alerts":[...]}. При любой ошибке — пустой список."""
    try:
        raw = text.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return []
            data = json.loads(m.group(0))
        alerts = data.get("alerts") if isinstance(data, dict) else None
        if not isinstance(alerts, list):
            return []
        out = []
        for a in alerts:
            if not isinstance(a, dict):
                continue
            ref = str(a.get("ref", "")).strip().strip("[]").lower()
            title = str(a.get("title", "")).strip()
            if not re.fullmatch(r"[nm]\d+", ref) or not title:
                continue
            out.append({"ref": ref, "title": title, "why": str(a.get("why", "")).strip()})
        return out[:3]
    except Exception:
        return []


def check_markets(quotes: list[Quote], levels: dict[str, int], now: datetime,
                  stale_hours: float) -> list[MarketAlert]:
    """Алерт, если |изм.| ≥ порога; повтор — только на следующем кратном пороге."""
    out = []
    for q in quotes:
        if not q.alert_pct or q.value is None or q.change_pct is None or q.as_of is None:
            continue
        if now - q.as_of > timedelta(hours=stale_hours):
            continue
        level = int(abs(q.change_pct) // q.alert_pct)
        if level >= 1 and level > levels.get(q.key, 0):
            arrow = "📈" if q.change_pct > 0 else "📉"
            text = (f"{arrow} <b>{html.escape(q.name)} {fmt_pct(q.change_pct)}</b> за день "
                    f"(сейчас {fmt_num(q.value)}; порог {q.alert_pct:g}%)")
            out.append(MarketAlert(q.key, level, text))
    return out


def _since(ctx) -> datetime:
    now = ctx.now()
    last = ctx.state["last_alert_check"]
    if not last:
        return now - timedelta(hours=float(ctx.cfg.get("alerts.first_check_lookback_hours", 1)))
    # небольшой запас: ленты публикуют с задержкой; уже виденное отсечёт seen_news
    return max(datetime.fromisoformat(last) - timedelta(minutes=30), now - timedelta(hours=12))


def classify(ctx, news: list[NewsItem], mail: list[MailItem], market: list[MarketAlert]) -> list[dict]:
    cfg = ctx.cfg
    tz = cfg.tz
    lines = [f"Сейчас {ctx.now().astimezone(tz):%d.%m %H:%M} МСК."]
    if market:
        lines += ["", "Резкие движения рынка (уже отправлены отдельно):"]
        lines += [f"- {re.sub(r'<[^>]+>', '', m.text)}" for m in market]
    if news:
        lines += ["", "Новые новости:"]
        for n in news:
            t = n.published.astimezone(tz).strftime("%H:%M") if n.published else "?"
            lines.append(f"[{n.ref}] {n.source} · {t} · {n.title}")
    if mail:
        lines += ["", "Новые письма:", mail_for_prompt(mail, bool(cfg.get("mail.send_body_to_claude", True)), tz)]
    system = ALERT_SYSTEM.format(profile=cfg.profile)
    kwargs = dict(purpose="alerts", model=cfg.get("models.alerts", "claude-haiku-5-5"), system=system,
                  messages=[{"role": "user", "content": "\n".join(lines)}], max_tokens=4000,
                  effort=cfg.get("models.alerts_effort", "low"))
    try:
        try:
            res = llm.ask(ctx, output_format=ALERT_SCHEMA, **kwargs)
        except llm.LLMError as e:
            if "отклонил" not in str(e):
                raise
            res = llm.ask(ctx, **kwargs)  # модель не поддерживает structured outputs
    except Exception as e:
        log.warning("фильтр срочного: %s", e)
        return []
    return parse_alerts(res.text)


def format_alerts(market: list[MarketAlert], picked: list[dict], refs: dict) -> list[str]:
    items = [m.text for m in market]
    for a in picked:
        obj = refs.get(a["ref"])
        if obj is None:
            continue
        line = f"• <b>{html.escape(a['title'])}</b>"
        if a["why"]:
            line += f" — {html.escape(a['why'])}"
        if isinstance(obj, NewsItem) and obj.link:
            line += f' <a href="{html.escape(obj.link, quote=True)}">{html.escape(obj.source)}</a>'
        elif isinstance(obj, MailItem):
            line += f" <i>(письмо: {html.escape(obj.sender)})</i>"
        items.append(line)
    return items


def run_alerts(ctx) -> list[str]:
    """Одна проверка. Возвращает отправленные пункты (пусто — срочного нет)."""
    cfg, st = ctx.cfg, ctx.state
    now = ctx.now()
    today = now.astimezone(cfg.tz).date().isoformat()
    max_day = int(cfg.get("alerts.max_per_day", 6))
    already = len(st.alerts_today(today))
    since = _since(ctx)

    quotes = fetch_quotes(ctx)
    levels = dict(st["market_levels"].get(today, {}))
    market = check_markets(quotes, levels, now, float(cfg.get("alerts.stale_market_hours", 6)))

    window = max((now - since).total_seconds() / 3600, 0.5)
    news, _failed = collect_news(ctx, window)
    seen_news = st.seen("news")
    fresh = [n for n in news if n.id not in seen_news and (n.published is None or n.published >= since)]
    mail: list[MailItem] = []
    if cfg.secrets.mail_enabled and cfg.get("alerts.include_mail", True):
        try:
            seen_mail = st.seen("mail")
            mail = [m for m in fetch_mail(cfg, since, ctx.imap_factory) if m.id not in seen_mail]
        except Exception as e:
            log.warning("почта для алертов: %s", e)

    refs: dict[str, object] = {}
    for i, n in enumerate(fresh, 1):
        n.ref = f"n{i}"
        refs[n.ref] = n
    for i, m in enumerate(mail, 1):
        m.ref = f"m{i}"
        refs[m.ref] = m

    room = max(max_day - already, 0)
    picked = classify(ctx, fresh, mail, market) if (fresh or mail) and room else []
    items = format_alerts(market, picked, refs)[:room]

    if items:
        text = "🚨 <b>Срочно</b>\n\n" + "\n\n".join(items)
        if ctx.dry_run or ctx.telegram is None:
            print(text)
        else:
            ctx.telegram.send(text)
    with st.lock:
        for m in market[: room]:
            levels[m.key] = m.level
        st["market_levels"] = {**st["market_levels"], today: levels}
        if items:
            day = st["alerts_sent"].get(today, [])
            day += [{"at": now.isoformat(), "text": t} for t in items]
            st["alerts_sent"] = {**st["alerts_sent"], today: day}
        st.mark_seen("news", [n.id for n in fresh])
        st.mark_seen("mail", [m.id for m in mail])
        st["last_alert_check"] = now.isoformat()
        st.save()
    log.info("алерты: рынок %d, новостей %d, писем %d → отправлено %d", len(market), len(fresh), len(mail), len(items))
    return items
