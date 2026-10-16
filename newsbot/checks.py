"""Проверка источников: ленты, котировки, почта, календарь, нейросеть (Claude/Gemini), Telegram."""
from __future__ import annotations

import html
from datetime import timedelta

from .feeds import collect_news
from .ical import fetch_events
from .llm import is_gemini
from .mail import fetch_mail
from .markets import fetch_quotes


def run_checks(ctx, claude: bool = True, telegram: bool = True) -> list[tuple[str, bool, str]]:
    cfg = ctx.cfg
    rows: list[tuple[str, bool, str]] = []

    for feed in cfg.feeds:
        items, failed = collect_news(ctx, None, feeds=[feed])
        if failed:
            rows.append((f"Лента: {feed.name}", False, failed[0][1]))
        else:
            rows.append((f"Лента: {feed.name}", bool(items), f"{len(items)} записей" if items else "пусто"))

    for q in fetch_quotes(ctx):
        if q.value is None:
            rows.append((f"Котировка: {q.name}", False, q.error or "нет данных"))
        else:
            rows.append((f"Котировка: {q.name}", True, q.line(cfg.tz).split(": ", 1)[1]))

    if cfg.secrets.mail_enabled:
        try:
            n = len(fetch_mail(cfg, ctx.now() - timedelta(days=1), ctx.imap_factory))
            rows.append(("Почта (IMAP)", True, f"непрочитанных за сутки: {n}"))
        except Exception as e:
            rows.append(("Почта (IMAP)", False, str(e) or type(e).__name__))
    else:
        rows.append(("Почта (IMAP)", True, "не настроена — пропускаю"))

    if cfg.secrets.ics_url:
        try:
            n = len(fetch_events(ctx, ctx.now().astimezone(cfg.tz).date()))
            rows.append(("Календарь (iCal)", True, f"событий сегодня: {n}"))
        except Exception as e:
            rows.append(("Календарь (iCal)", False, str(e) or type(e).__name__))
    else:
        rows.append(("Календарь (iCal)", True, "не настроен — пропускаю"))

    if claude:
        models = list(dict.fromkeys(cfg.get(k) for k in ("models.digest", "models.chat", "models.alerts")))
        for model in [m for m in models if m]:
            rows.append(_check_gemini(ctx, model) if is_gemini(model) else _check_claude(ctx, model))

    if telegram:
        if ctx.telegram is None or not cfg.secrets.telegram_bot_token:
            rows.append(("Telegram", False, "TELEGRAM_BOT_TOKEN не задан"))
        else:
            try:
                me = ctx.telegram.get_me()
                chat = cfg.secrets.telegram_chat_id or "не задан — пришлите боту /start"
                rows.append(("Telegram", True, f"@{me.get('username')}, chat_id: {chat}"))
            except Exception as e:
                rows.append(("Telegram", False, str(e)))
    return rows


def _check_claude(ctx, model: str) -> tuple[str, bool, str]:
    if ctx.llm is None:
        return ("Claude API", False, "ANTHROPIC_API_KEY не задан")
    try:
        ctx.llm.models.retrieve(model)  # бесплатно: проверяет ключ, страну и имя модели
        return (f"Claude API: {model}", True, "доступна")
    except Exception as e:
        return (f"Claude API: {model}", False, _api_error(e))


def _check_gemini(ctx, model: str) -> tuple[str, bool, str]:
    if getattr(ctx, "gemini", None) is None:
        return ("Gemini API", False, "GEMINI_API_KEY не задан")
    try:
        ctx.gemini.get_model(model)  # бесплатно: проверяет ключ, страну и имя модели
        return (f"Gemini API: {model}", True, "доступна")
    except Exception as e:
        status = getattr(e, "status_code", None)
        msg = getattr(e, "message", None) or str(e)
        if status == 400 and "key" in msg.lower() or status == 401:
            return (f"Gemini API: {model}", False, "неверный GEMINI_API_KEY")
        if status == 403:
            return (f"Gemini API: {model}", False, f"403 — нет доступа или регион не поддерживается ({msg[:150]})")
        if status == 404:
            return (f"Gemini API: {model}", False, "404 — модель не найдена, проверьте имя в config.yaml")
        return (f"Gemini API: {model}", False, f"{status or type(e).__name__}: {msg[:200]}")


def _api_error(e: Exception) -> str:
    status = getattr(e, "status_code", None)
    msg = getattr(e, "message", None) or str(e) or type(e).__name__
    if status == 403:
        return f"403 — регион сервера не поддерживается или нет доступа ({msg[:150]})"
    if status == 401:
        return "401 — неверный ANTHROPIC_API_KEY"
    if status == 404:
        return "404 — модель не найдена, проверьте имя в config.yaml"
    return f"{status or type(e).__name__}: {msg[:200]}"


def format_report(rows, html_mode: bool = False) -> str:
    lines = []
    for name, ok, detail in rows:
        mark = "✅" if ok else "❌"
        if html_mode:
            lines.append(f"{mark} <b>{html.escape(name)}</b> — {html.escape(detail)}")
        else:
            lines.append(f"{mark} {name} — {detail}")
    bad = sum(1 for _, ok, _ in rows if not ok)
    lines.append("")
    lines.append("Всё работает." if not bad else f"Проблем: {bad}.")
    return "\n".join(lines)
