"""Командная строка: serve | digest | alerts | check | chat-id."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .context import Ctx, make_anthropic, make_http
from .logs import setup_logging
from .state import State
from .telegram import Telegram, TelegramError

log = logging.getLogger("newsbot")


def build_ctx(config: str, dry_run: bool = False, log_to_file: bool = True) -> Ctx:
    cfg = load_config(config)
    s = cfg.secrets
    setup_logging(cfg.data_dir, [s.telegram_bot_token, s.anthropic_api_key, s.imap_password, s.ics_url],
                  to_file=log_to_file and not dry_run)
    http = make_http(cfg.get("http.timeout", 15))
    state = State.load(cfg.data_dir / "state.json", readonly=dry_run)
    tg = Telegram(http, s.telegram_bot_token, s.telegram_chat_id, cfg.get("telegram.http_timeout", 20)) \
        if s.telegram_bot_token else None
    return Ctx(cfg=cfg, state=state, http=http, llm=make_anthropic(s.anthropic_api_key),
               telegram=tg, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="main.py", description="Личный новостной агент в Telegram")
    p.add_argument("--config", default=str(Path(__file__).resolve().parent.parent / "config.yaml"))
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="бот + расписание (по умолчанию)")
    d = sub.add_parser("digest", help="сводка сейчас")
    d.add_argument("kind", nargs="?", default="auto", choices=["morning", "evening", "auto"])
    d.add_argument("--dry-run", action="store_true", help="напечатать в консоль, не трогать state")
    a = sub.add_parser("alerts", help="проверить срочное сейчас")
    a.add_argument("--dry-run", action="store_true")
    sub.add_parser("check", help="проверить все источники и ключи")
    sub.add_parser("chat-id", help="показать chat_id из последних сообщений боту")
    args = p.parse_args(argv)
    cmd = args.cmd or "serve"
    dry = bool(getattr(args, "dry_run", False))

    try:
        ctx = build_ctx(args.config, dry_run=dry, log_to_file=cmd == "serve")
    except ConfigError as e:
        print(f"Ошибка в настройках: {e}", file=sys.stderr)
        return 2

    if cmd == "serve":
        if ctx.telegram is None:
            print("TELEGRAM_BOT_TOKEN не задан", file=sys.stderr)
            return 2
        from .bot import serve

        serve(ctx)
        return 0
    if cmd == "digest":
        from .digest import run_digest

        with ctx.heavy_lock:
            res = run_digest(ctx, args.kind, scheduled=False)
        if res.fallback:
            print(f"\n[запасная сводка: {res.error}]", file=sys.stderr)
        else:
            print(f"\n[примерная стоимость: ${res.usd:.3f}]", file=sys.stderr)
        return 0
    if cmd == "alerts":
        from .alerts import run_alerts

        items = run_alerts(ctx)
        if not items:
            print("Срочного нет ✅")
        return 0
    if cmd == "check":
        from .checks import format_report, run_checks

        rows = run_checks(ctx)
        print(format_report(rows))
        return 0 if all(ok for _, ok, _ in rows) else 1
    if cmd == "chat-id":
        if ctx.telegram is None:
            print("TELEGRAM_BOT_TOKEN не задан", file=sys.stderr)
            return 2
        try:
            updates = ctx.telegram.call("getUpdates", {"timeout": 0})
        except TelegramError as e:
            print(f"Telegram: {e} (если 409 — бот уже запущен, остановите его)", file=sys.stderr)
            return 1
        chats = {}
        for u in updates or []:
            chat = (u.get("message") or {}).get("chat") or {}
            if chat:
                chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
        if not chats:
            print("Сообщений нет. Напишите боту что-нибудь в Telegram и повторите.")
        for cid, name in chats.items():
            print(f"chat_id: {cid}  {name}")
        return 0
    return 1
