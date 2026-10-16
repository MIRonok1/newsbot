import httpx

from newsbot.digest import build_input, collect, resolve_refs, run_digest, window_hours
from tests.conftest import FIXTURES, NOW, make_ctx
from tests.fakes import FakeAnthropic, response
from tests.test_markets import CBR_JSON, ISS_INDEX, YAHOO

RBC = (FIXTURES / "rbc.xml").read_bytes()
GN = (FIXTURES / "gnews.xml").read_bytes()


def handler(req: httpx.Request):
    url = str(req.url)
    if "rbc" in url:
        return httpx.Response(200, content=RBC)
    if "news.google" in url:
        return httpx.Response(200, content=GN)
    if "iss.moex" in url:
        return httpx.Response(200, json=ISS_INDEX)
    if "cbr-xml-daily" in url:
        return httpx.Response(200, json=CBR_JSON)
    if "yahoo" in url:
        return httpx.Response(200, json=YAHOO)
    if "kommersant" in url:
        raise httpx.ReadTimeout("slow")
    return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>')


MODEL_OUT = """Сначала поищу детали.
<digest><b>☀️ Утренняя сводка · пт, 16 октября</b>

<b>🇷🇺 Россия, ЦБ и рынок</b>
• <b>ЦБ сохранил ставку 16%.</b> Детали. → важно. <a href="n1">Интерфакс</a>
• <b>Поиск.</b> Факт. <a href="https://example.com/x">Reuters</a>
• <b>Фантом.</b> <a href="n999">нет</a> <script>
</digest>"""


def test_full_dry_run(tmp_path, capsys):
    fake = FakeAnthropic([response(MODEL_OUT, searches=2)])
    ctx = make_ctx(tmp_path, handler, llm=fake, dry_run=True)
    res = run_digest(ctx, "auto", scheduled=True)
    assert res.kind == "morning" and not res.fallback
    out = capsys.readouterr().out
    assert "Утренняя сводка" in out
    assert 'href="https://rbc.ru/a1"' in out or 'href="https://news.google.com/rss/articles/abc"' in out
    assert 'href="https://example.com/x"' in out
    assert "n999" not in out and "&lt;script&gt;" in out
    assert "<i>Не загрузились: Коммерсантъ (таймаут)</i>" in out
    assert "Сначала поищу" not in out
    # Вход модели: рубрики по порядку, номера, прошлое окно, котировки
    user = fake.calls[0]["messages"][0]["content"]
    assert user.index("### 🇷🇺") < user.index("### 🪙") < user.index("### 🤖") < user.index("### 🌍")
    assert "[n1]" in user and "Индекс Мосбиржи: 2" in user
    assert "rbc.ru/a1" not in user  # ссылки во вход не идут — экономия токенов
    system = fake.calls[0]["system"]
    assert "Дмитрий" in system and "пт, 16 октября" in system and "Brent 4%" in system
    # dry-run не меняет state и не пишет файл
    assert not (ctx.cfg.data_dir / "state.json").exists()


def test_fallback_when_claude_fails(tmp_path, capsys):
    fake = FakeAnthropic([RuntimeError("API down")])
    ctx = make_ctx(tmp_path, handler, llm=fake)
    sent = []
    ctx.telegram = type("T", (), {"send": lambda self, t: sent.append(t)})()
    res = run_digest(ctx, "evening", scheduled=True)
    assert res.fallback and "API down" in res.text
    assert "🌙 Вечерняя сводка" in sent[0] and "Индекс Мосбиржи" in sent[0]
    assert 'href="https://rbc.ru/a1"' in sent[0] or "news.google.com" in sent[0]
    # state обновлён: окно и текст
    st = ctx.state
    assert st["last_scheduled_digest_at"] == NOW.isoformat()
    assert st["last_digest_text"] == res.text and st["seen_news"]
    assert (ctx.cfg.data_dir / "state.json").exists()


def test_window_clamped(tmp_path):
    ctx = make_ctx(tmp_path)
    assert window_hours(ctx) == 12
    ctx.state["last_scheduled_digest_at"] = "2026-10-16T03:00:00+00:00"
    assert window_hours(ctx) == 6
    ctx.state["last_scheduled_digest_at"] = "2026-10-10T03:00:00+00:00"
    assert window_hours(ctx) == 24
    ctx.state["last_scheduled_digest_at"] = "2026-10-15T17:00:00+00:00"
    assert window_hours(ctx) == 12


def test_prev_digest_in_input(tmp_path):
    ctx = make_ctx(tmp_path, handler)
    ctx.state["last_digest_text"] = '<b>Прошлое</b>\n• пункт <a href="https://x">src</a>'
    data = collect(ctx, "morning")
    text = build_input(ctx, data)
    assert "## Прошлая сводка" in text and "Прошлое" in text and "https://x" not in text


def test_resolve_refs():
    assert resolve_refs('<a href="n2">a</a> <a href=\'[N2]\'>b</a>', {"n2": "https://u?a=1&b=2"}) == \
        '<a href="https://u?a=1&amp;b=2">a</a> <a href="https://u?a=1&amp;b=2">b</a>'
