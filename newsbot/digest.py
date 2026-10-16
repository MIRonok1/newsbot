"""Сводка: сбор данных → вход модели → Claude → санитайзер → Telegram. Запасная сводка при сбое."""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import llm
from .feeds import NewsItem, collect_news
from .ical import Event, fetch_events
from .mail import MailItem, fetch_mail, mail_for_prompt
from .markets import Quote, fetch_quotes
from .prompts import digest_system
from .sanitize import sanitize_html
from .textutil import html_to_text, ru_date, truncate
from .usage import budget_mode

log = logging.getLogger(__name__)


@dataclass
class DigestData:
    kind: str
    now: datetime
    window_hours: float
    window_start: datetime
    news: list[NewsItem]
    quotes: list[Quote]
    mail: list[MailItem]
    events: list[Event]
    failed: list[tuple[str, str]] = field(default_factory=list)
    refs: dict[str, str] = field(default_factory=dict)  # n12 -> url


@dataclass
class DigestResult:
    text: str
    kind: str
    fallback: bool
    error: str | None = None
    usd: float = 0.0


def resolve_kind(kind: str, now_local: datetime) -> str:
    if kind in ("morning", "evening"):
        return kind
    return "morning" if now_local.hour < 14 else "evening"


def window_hours(ctx) -> float:
    cfg, now = ctx.cfg, ctx.now()
    lo = float(cfg.get("digest.window_min_hours", 6))
    hi = float(cfg.get("digest.window_max_hours", 24))
    last = ctx.state["last_scheduled_digest_at"]
    if not last:
        return 12.0 if lo <= 12 <= hi else hi
    hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600
    return min(max(hours, lo), hi)


def collect(ctx, kind: str) -> DigestData:
    cfg = ctx.cfg
    now = ctx.now()
    local = now.astimezone(cfg.tz)
    hours = window_hours(ctx)
    start = now - timedelta(hours=hours)
    news, failed = collect_news(ctx, hours)
    quotes = fetch_quotes(ctx)
    mail: list[MailItem] = []
    if cfg.secrets.mail_enabled:
        try:
            mail = fetch_mail(cfg, min(start, now - timedelta(hours=12)), ctx.imap_factory)
        except Exception as e:
            log.warning("почта: %s", e)
            failed.append(("Почта", str(e) or type(e).__name__))
    events: list[Event] = []
    if cfg.secrets.ics_url:
        day = local.date() if kind == "morning" else local.date() + timedelta(days=1)
        try:
            events = fetch_events(ctx, day)
        except Exception as e:
            log.warning("календарь: %s", e)
            failed.append(("Календарь", str(e) or type(e).__name__))
    data = DigestData(kind, now, hours, start, news, quotes, mail, events, failed)
    _assign_refs(data, cfg)
    return data


def _assign_refs(data: DigestData, cfg) -> None:
    order = {r["key"]: i for i, r in enumerate(cfg.rubrics)}
    data.news.sort(key=lambda n: (order.get(n.topic, 99), n.priority != "high",
                                  -(n.published.timestamp() if n.published else 0)))
    for i, n in enumerate(data.news, 1):
        n.ref = f"n{i}"
        data.refs[n.ref] = n.link
    for i, m in enumerate(data.mail, 1):
        m.ref = f"m{i}"


def build_input(ctx, data: DigestData) -> str:
    cfg = ctx.cfg
    tz = cfg.tz
    local = data.now.astimezone(tz)
    lines = [
        f"Сейчас: {ru_date(local)} {local.year}, {local:%H:%M} МСК. Сводка: "
        f"{'утренняя' if data.kind == 'morning' else 'вечерняя'}.",
        f"Окно новостей: с {data.window_start.astimezone(tz):%d.%m %H:%M} по {local:%d.%m %H:%M} МСК "
        f"({data.window_hours:.0f} ч).",
        "",
        "## Котировки",
    ]
    lines += [f"- {q.line(tz)}" for q in data.quotes] or ["- нет данных"]
    lines += ["", "## Новости по рубрикам"]
    for r in cfg.rubrics:
        items = [n for n in data.news if n.topic == r["key"]]
        lines += ["", f"### {r['title']}"]
        if not items:
            lines.append("(в лентах ничего)")
        for n in items:
            t = n.published.astimezone(tz).strftime("%d.%m %H:%M") if n.published else "?"
            mark = " [приоритет]" if n.priority == "high" else ""
            lines.append(f"[{n.ref}] {n.source} · {t}{mark} · {n.title}")
            if n.summary:
                lines.append(f"    {n.summary}")
    if data.mail:
        lines += ["", "## Почта (непрочитанные)",
                  mail_for_prompt(data.mail, bool(cfg.get("mail.send_body_to_claude", True)), tz)]
    if data.events:
        day = data.now.astimezone(tz) + (timedelta(0) if data.kind == "morning" else timedelta(days=1))
        lines += ["", f"## Календарь на {ru_date(day)}"]
        lines += [f"- {e.line(tz)}" for e in data.events]
    prev = ctx.state["last_digest_text"]
    if prev:
        lines += ["", "## Прошлая сводка (не повторять без новых деталей)",
                  truncate(html_to_text(prev, keep_lines=True), 5000)]
    return "\n".join(lines)


def resolve_refs(text: str, refs: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        url = refs.get(m.group(1).lower())
        return f'href="{html.escape(url, quote=True)}"' if url else 'href=""'

    return re.sub(r'href\s*=\s*["\']\[?([nNmM]\d+)\]?["\']', repl, text)


def failed_footer(failed: list[tuple[str, str]]) -> str:
    if not failed:
        return ""
    parts = ", ".join(f"{html.escape(n)} ({html.escape(e[:60])})" for n, e in failed)
    return f"\n\n<i>Не загрузились: {parts}</i>"


def fallback_digest(ctx, data: DigestData, error: str) -> str:
    tz = ctx.cfg.tz
    local = data.now.astimezone(tz)
    title = "☀️ Утренняя сводка" if data.kind == "morning" else "🌙 Вечерняя сводка"
    out = [f"<b>{title} · {ru_date(local)}</b>",
           f"<i>Запасной вариант: Claude недоступен — {html.escape(error[:300])}</i>", "",
           "<b>📈 Рынки</b>"]
    out += [f"• {html.escape(q.line(tz))}" for q in data.quotes]
    for r in ctx.cfg.rubrics:
        items = [n for n in data.news if n.topic == r["key"]][:5]
        out += ["", f"<b>{html.escape(r['title'])}</b>"]
        if not items:
            out.append("существенного не было")
        for n in items:
            link = f' <a href="{html.escape(n.link, quote=True)}">{html.escape(n.source)}</a>' if n.link else ""
            out.append(f"• {html.escape(n.title, quote=False)}.{link}")
    if data.events:
        out += ["", "<b>📅 " + ("Сегодня" if data.kind == "morning" else "Завтра") + "</b>"]
        out += [f"• {html.escape(e.line(tz))}" for e in data.events]
    if data.mail:
        out += ["", "<b>📬 Почта</b>"]
        out += [f"• {html.escape(m.sender)}: «{html.escape(m.subject)}»" for m in data.mail[:10]]
    return "\n".join(out)


def generate(ctx, data: DigestData) -> DigestResult:
    cfg = ctx.cfg
    model = cfg.get("models.digest", "claude-sonnet-5-5")
    uses = int(cfg.get("web_search.digest_max_uses", 4))
    search_on = bool(cfg.get("web_search.enabled", True)) and uses > 0 and \
        budget_mode(cfg, ctx.now()) == "ok"
    system = digest_system(cfg, data.kind, data.now.astimezone(cfg.tz), uses, search_on)
    try:
        res = llm.ask(ctx, purpose=f"digest_{data.kind}", model=model, system=system,
                      messages=[{"role": "user", "content": build_input(ctx, data)}],
                      max_tokens=16000, effort=cfg.get("models.digest_effort", "medium"),
                      search_uses=uses if search_on else 0)
        body = llm.extract_digest(res.text)
        if not body:
            if "•" in res.text and len(res.text) > 500:
                body = res.text.strip()
            else:
                raise llm.LLMError(f"модель не вернула сводку (stop_reason={res.stop_reason})")
        if res.stop_reason == "max_tokens":
            body += "\n\n<i>(сводка обрезана по лимиту длины)</i>"
        text = sanitize_html(resolve_refs(body, data.refs))
        for note in res.notes:
            log.info("digest: %s", note)
        return DigestResult(text, data.kind, False, usd=res.usd)
    except Exception as e:
        log.exception("сводка: Claude не сработал")
        msg = str(e) or type(e).__name__
        return DigestResult(fallback_digest(ctx, data, msg), data.kind, True, error=msg)


def run_digest(ctx, kind: str = "auto", scheduled: bool = False) -> DigestResult:
    """Полный цикл. В dry-run печатает в консоль и не трогает state."""
    now = ctx.now()
    kind = resolve_kind(kind, now.astimezone(ctx.cfg.tz))
    data = collect(ctx, kind)
    result = generate(ctx, data)
    if ctx.cfg.get("digest.show_failed_feeds", True):
        result.text += failed_footer(data.failed)
    if ctx.dry_run or ctx.telegram is None:
        print(result.text)
    else:
        ctx.telegram.send(result.text)
    st = ctx.state
    with st.lock:
        st["last_digest_at"] = {**st["last_digest_at"], kind: now.isoformat()}
        if scheduled:
            st["last_scheduled_digest_at"] = now.isoformat()
        st["last_digest_text"] = result.text
        st["last_digest_kind"] = kind
        st.mark_seen("news", [n.id for n in data.news])
        st.mark_seen("mail", [m.id for m in data.mail])
        st.save()  # в dry-run state.readonly → ничего не пишется
    log.info("сводка %s: %d новостей, fallback=%s, $%.3f", kind, len(data.news), result.fallback, result.usd)
    return result
