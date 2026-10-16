import json
from datetime import datetime, timedelta, timezone

import httpx

from newsbot.bot import Bot
from newsbot.context import make_http
from newsbot.scheduler import build_scheduler, missed_digest
from newsbot.telegram import Telegram
from tests.conftest import NOW, make_ctx
from tests.fakes import FakeAnthropic, response


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text, chat_id=None, html=True):
        self.sent.append((str(chat_id) if chat_id else None, text))
        return 1

    def typing(self, chat_id=None):
        pass


def upd(text, chat=42, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_only_owner_chat(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.telegram = FakeTG()
    bot = Bot(ctx)
    bot.handle(upd("/help", chat=999))
    assert ctx.telegram.sent == []
    bot.handle(upd("/help"))
    assert "/digest" in ctx.telegram.sent[0][1]


def test_start_reveals_chat_id_when_unset(tmp_path):
    from newsbot.config import Secrets

    ctx = make_ctx(tmp_path, secrets=Secrets(telegram_bot_token="1:x"))
    ctx.telegram = FakeTG()
    bot = Bot(ctx)
    bot.handle(upd("hello", chat=777))
    assert ctx.telegram.sent == []
    bot.handle(upd("/start", chat=777))
    assert ctx.telegram.sent == [("777", ctx.telegram.sent[0][1])] and "777" in ctx.telegram.sent[0][1]


def test_question_uses_digest_and_history(tmp_path):
    fake = FakeAnthropic([response("<b>Ставка</b> — это… <a href=\"https://cbr.ru\">ЦБ</a>")])
    ctx = make_ctx(tmp_path, llm=fake)
    ctx.telegram = FakeTG()
    ctx.state["last_digest_text"] = "<b>Сводка</b>\n• ЦБ сохранил ставку"
    ctx.state["chat_history"] = [{"q": f"q{i}", "a": f"a{i}"} for i in range(5)]
    reply = Bot(ctx).answer("объясни подробнее про ставку")
    call = fake.calls[0]
    assert "ЦБ сохранил ставку" in call["system"]
    assert [m["content"] for m in call["messages"]][:2] == ["q2", "a2"] and len(call["messages"]) == 7
    assert call["tools"][0]["max_uses"] == 3
    assert reply.startswith("<b>Ставка</b>") and ctx.telegram.sent
    assert ctx.state["chat_history"][-1]["q"] == "объясни подробнее про ставку"


def test_heavy_lock_busy(tmp_path):
    ctx = make_ctx(tmp_path)
    ctx.telegram = FakeTG()
    ctx.heavy_lock.acquire()
    Bot(ctx).handle(upd("/digest"))
    assert "Уже готовлю" in ctx.telegram.sent[0][1]


def test_status(tmp_path):
    ctx = make_ctx(tmp_path, llm=FakeAnthropic([response("x", inp=1_000_000)]))
    from newsbot import llm
    llm.ask(ctx, purpose="t", model="claude-sonnet-5-5", system="", messages=[{"role": "user", "content": "x"}],
            max_tokens=10)
    ctx.state["last_digest_at"] = {"morning": NOW.isoformat()}
    ctx.telegram = FakeTG()
    sched = build_scheduler(ctx)
    sched.start(paused=True)
    try:
        text = Bot(ctx, sched).status_text()
    finally:
        sched.shutdown(wait=False)
    assert "Утренняя сводка: 16.10 08:00" in text
    assert "$2.00</b> из $10" in text and "Вызовов: 1, поисков: 0" in text
    assert "Следующий запуск" in text


def test_scheduler_jobs(tmp_path):
    ctx = make_ctx(tmp_path)
    sched = build_scheduler(ctx)
    sched.start(paused=True)
    try:
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {"digest_morning", "digest_evening", "alerts"}
        assert jobs["digest_morning"].misfire_grace_time == 3600
        t = jobs["alerts"].trigger
        nxt = t.get_next_fire_time(None, datetime(2026, 10, 16, 8, 0, tzinfo=ctx.cfg.tz))
        assert (nxt.hour, nxt.minute) == (8, 15)
        nxt = t.get_next_fire_time(None, datetime(2026, 10, 16, 23, 50, tzinfo=ctx.cfg.tz))
        assert (nxt.day, nxt.hour, nxt.minute) == (17, 7, 15)
    finally:
        sched.shutdown(wait=False)


def test_missed_digest_catchup(tmp_path):
    ctx = make_ctx(tmp_path)
    tz = ctx.cfg.tz
    assert missed_digest(ctx, datetime(2026, 10, 16, 8, 40, tzinfo=tz)) == "morning"
    assert missed_digest(ctx, datetime(2026, 10, 16, 9, 30, tzinfo=tz)) is None
    ctx.state["last_digest_at"] = {"morning": datetime(2026, 10, 16, 8, 0, 5, tzinfo=tz).isoformat()}
    assert missed_digest(ctx, datetime(2026, 10, 16, 8, 40, tzinfo=tz)) is None
    assert missed_digest(ctx, datetime(2026, 10, 16, 20, 10, tzinfo=tz)) == "evening"


def test_polling_offset_and_409(tmp_path):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if req.url.path.endswith("getUpdates"):
            if len([c for c in calls if "offset" in c]) == 1:
                return httpx.Response(200, json={"ok": True, "result": [upd("/help", uid=10)]})
            return httpx.Response(409, json={"ok": False, "error_code": 409, "description": "Conflict"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    ctx = make_ctx(tmp_path)
    ctx.telegram = Telegram(make_http(5, httpx.MockTransport(handler)), "1:x", "42")
    import threading

    stop = threading.Event()
    bot = Bot(ctx)
    stop.wait = lambda s: stop.set()  # первый 409 → выходим
    bot.run(stop)
    assert ctx.state["tg_offset"] == 11
    assert any(c.get("text", "").startswith("<b>Личный") for c in calls)
