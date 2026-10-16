"""Long polling, команды и вопросы обычным сообщением."""
from __future__ import annotations

import html
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import llm
from .alerts import run_alerts
from .checks import format_report, run_checks
from .digest import run_digest
from .prompts import CHAT_SYSTEM
from .sanitize import sanitize_html
from .telegram import TelegramError
from .textutil import html_to_text, ru_date, truncate
from .usage import budget_mode, month_usage

log = logging.getLogger(__name__)

HELP = """<b>Личный новостной агент</b>

Сводки приходят в {morning} и {evening} МСК, срочное — между ними.

/digest — сводка прямо сейчас
/alerts — проверить срочное сейчас
/status — когда были сводки, расходы API
/sources — проверить все источники
/help — эта справка

Любой другой текст — вопрос: отвечу с учётом последней сводки («объясни подробнее про ставку»)."""


class Bot:
    def __init__(self, ctx, scheduler=None):
        self.ctx = ctx
        self.tg = ctx.telegram
        self.scheduler = scheduler
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
        self.chat_id = str(ctx.cfg.secrets.telegram_chat_id)

    # --- цикл -------------------------------------------------------------------
    def run(self, stop: threading.Event) -> None:
        timeout = int(self.ctx.cfg.get("telegram.poll_timeout", 50))
        while not stop.is_set():
            try:
                updates = self.tg.get_updates(self.ctx.state["tg_offset"], timeout)
            except TelegramError as e:
                if e.code == 409:
                    log.error("409 Conflict: запущена вторая копия бота с тем же токеном. Жду 30 с")
                    stop.wait(30)
                else:
                    log.warning("getUpdates: %s", e)
                    stop.wait(5)
                continue
            for upd in updates:
                with self.ctx.state.lock:
                    self.ctx.state["tg_offset"] = upd["update_id"] + 1
                    self.ctx.state.save()
                try:
                    self.handle(upd)
                except Exception:
                    log.exception("обработка сообщения")

    def handle(self, upd: dict) -> None:
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not chat or not text:
            return
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if not self.chat_id:
            if cmd == "/start":
                self.tg.send(f"Ваш chat_id: <code>{chat}</code>\nВпишите его в TELEGRAM_CHAT_ID и перезапустите бота.",
                             chat_id=chat)
            return
        if chat != self.chat_id:
            log.info("сообщение из чужого чата %s проигнорировано", chat)
            return
        if cmd in ("/start", "/help"):
            self.tg.send(HELP.format(morning=self.ctx.cfg.get("schedule.morning"),
                                     evening=self.ctx.cfg.get("schedule.evening")))
        elif cmd == "/digest":
            self._heavy("Готовлю сводку, это займёт пару минут…", self._digest)
        elif cmd == "/alerts":
            self._heavy(None, self._alerts)
        elif cmd == "/status":
            self.tg.send(self.status_text())
        elif cmd == "/sources":
            self.tg.send("Проверяю источники…")
            self.pool.submit(self._safe, self._sources)
        elif cmd:
            self.tg.send("Не знаю такой команды. /help — список.")
        else:
            self.tg.typing()
            self.pool.submit(self._safe, self.answer, text)

    # --- фоновые задачи -----------------------------------------------------------
    def _heavy(self, notice: str | None, fn) -> None:
        if not self.ctx.heavy_lock.acquire(blocking=False):
            self.tg.send("Уже готовлю сводку или проверяю срочное — подождите минуту.")
            return
        if notice:
            self.tg.send(notice)

        def job():
            try:
                fn()
            finally:
                self.ctx.heavy_lock.release()

        self.pool.submit(self._safe, job)

    def _safe(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            log.exception("фоновая задача")
            try:
                self.tg.send(f"⚠️ Ошибка: {html.escape(str(e)[:300])}")
            except Exception:
                pass

    def _digest(self) -> None:
        run_digest(self.ctx, "auto", scheduled=False)

    def _alerts(self) -> None:
        if not run_alerts(self.ctx):
            self.tg.send("Срочного нет ✅")

    def _sources(self) -> None:
        self.tg.send(format_report(run_checks(self.ctx, claude=True, telegram=False), html_mode=True))

    # --- вопросы --------------------------------------------------------------------
    def answer(self, question: str) -> str:
        ctx = self.ctx
        cfg = ctx.cfg
        now = ctx.now().astimezone(cfg.tz)
        digest = ctx.state["last_digest_text"]
        digest_plain = truncate(html_to_text(digest, keep_lines=True), 8000) if digest else "(сводок ещё не было)"
        uses = int(cfg.get("web_search.chat_max_uses", 3))
        system = CHAT_SYSTEM.format(profile=cfg.profile, search_limit=uses,
                                    now=f"{ru_date(now)} {now:%H:%M} МСК", digest=digest_plain)
        history = ctx.state["chat_history"][-int(cfg.get("chat.history_exchanges", 3)):]
        messages = []
        for h in history:
            messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
        messages.append({"role": "user", "content": question})
        try:
            res = llm.ask(ctx, purpose="chat", model=cfg.get("models.chat", "claude-sonnet-5-5"),
                          system=system, messages=messages, max_tokens=8000,
                          effort=cfg.get("models.chat_effort", "low"), search_uses=uses)
            reply = sanitize_html(res.text.strip()) or "Не получилось сформулировать ответ."
        except llm.LLMError as e:
            reply = f"Не могу ответить: {html.escape(str(e))}"
            self.tg.send(reply)
            return reply
        self.tg.send(reply)
        with ctx.state.lock:
            hist = ctx.state["chat_history"] + [{"q": question, "a": truncate(html_to_text(reply), 2000)}]
            ctx.state["chat_history"] = hist[-10:]
            ctx.state.save()
        return reply

    # --- статус ---------------------------------------------------------------------
    def status_text(self) -> str:
        ctx = self.ctx
        cfg = ctx.cfg
        tz = cfg.tz
        now = ctx.now()
        st = ctx.state

        def fmt(iso):
            return datetime.fromisoformat(iso).astimezone(tz).strftime("%d.%m %H:%M") if iso else "—"

        lines = ["<b>Статус</b>", ""]
        last = st["last_digest_at"]
        lines.append(f"Утренняя сводка: {fmt(last.get('morning'))}")
        lines.append(f"Вечерняя сводка: {fmt(last.get('evening'))}")
        if self.scheduler:
            for job in self.scheduler.get_jobs():
                if job.next_run_time:
                    lines.append(f"Следующий запуск «{html.escape(job.name)}»: "
                                 f"{job.next_run_time.astimezone(tz):%d.%m %H:%M}")
        today = now.astimezone(tz).date().isoformat()
        lines.append(f"Срочных сегодня: {len(st.alerts_today(today))} из {cfg.get('alerts.max_per_day', 6)}")
        lines.append(f"Последняя проверка срочного: {fmt(st['last_alert_check'])}")
        m = month_usage(cfg.data_dir / "usage.jsonl", now)
        budget = float(cfg.get("budget.monthly_usd", 0) or 0)
        mode = {"ok": "", "no_search": " — поиск отключён (порог бюджета)",
                "stopped": " — Claude отключён до конца месяца"}[budget_mode(cfg, now)]
        lines += ["", f"Расход API за {now.astimezone(tz):%m.%Y}: <b>${m.usd:.2f}</b>"
                      + (f" из ${budget:g}" if budget else "") + mode,
                  f"Вызовов: {m.calls}, поисков: {m.searches}, токенов: "
                  f"{m.input_tokens:,} вх / {m.output_tokens:,} вых".replace(",", "\u00a0").replace("\u00a0 ", ", ")]
        return "\n".join(lines)


def serve(ctx) -> None:
    from . import scheduler as sched_mod

    sched = sched_mod.start(ctx)
    bot = Bot(ctx, sched)
    stop = threading.Event()
    log.info("бот запущен")
    try:
        bot.run(stop)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        sched.shutdown(wait=False)
        bot.pool.shutdown(wait=False, cancel_futures=True)
        time.sleep(0.1)
