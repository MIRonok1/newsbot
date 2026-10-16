from datetime import datetime, timedelta, timezone

import httpx

from newsbot.alerts import check_markets, parse_alerts, run_alerts
from newsbot.markets import Quote
from tests.conftest import FIXTURES, NOW, make_ctx
from tests.fakes import FakeAnthropic, response

GN = (FIXTURES / "gnews.xml").read_bytes()


def q(pct, age_h=0.2, key="IMOEX", thr=2.5):
    return Quote(key=key, name="Индекс Мосбиржи", value=2700.0, change_pct=pct,
                 as_of=NOW - timedelta(hours=age_h), alert_pct=thr)


def test_parse_alerts_robust():
    assert parse_alerts('{"alerts":[{"ref":"n3","title":"ЦБ поднял ставку","why":"x"}]}')[0]["ref"] == "n3"
    assert parse_alerts('Вот ответ:\n```json\n{"alerts":[{"ref":"[M2]","title":"Дедлайн"}]}\n```')[0]["ref"] == "m2"
    assert parse_alerts('{"alerts":[]}') == []
    assert parse_alerts("не json") == []
    assert parse_alerts('{"alerts":"oops"}') == []
    assert parse_alerts('{"alerts":[{"ref":"zzz","title":"t"},{"ref":"n1"}]}') == []
    assert parse_alerts("[1,2]") == []


def test_market_thresholds_and_repeats():
    levels = {}
    assert check_markets([q(-2.0)], levels, NOW, 6) == []
    a = check_markets([q(-2.6)], levels, NOW, 6)
    assert len(a) == 1 and a[0].level == 1 and "−2,60%" in a[0].text
    levels["IMOEX"] = 1
    assert check_markets([q(-4.9)], levels, NOW, 6) == []
    assert check_markets([q(5.1)], levels, NOW, 6)[0].level == 2


def test_market_stale_skipped():
    assert check_markets([q(-9, age_h=30)], {}, NOW, 6) == []
    assert check_markets([Quote(key="USD", name="USD", value=90, change_pct=5, as_of=NOW)], {}, NOW, 6) == []


def _handler(req):
    if "news.google" in str(req.url):
        return httpx.Response(200, content=GN)
    if "iss.moex" in str(req.url) and "IMOEX" in str(req.url):
        return httpx.Response(200, json={"marketdata": {
            "columns": ["SECID", "CURRENTVALUE", "LASTCHANGEPRC", "SYSTIME"],
            "data": [["IMOEX", 2700, -3.1, "2026-10-16 07:50:00"]]}})
    return httpx.Response(404)


def test_run_alerts_flow(tmp_path, capsys):
    fake = FakeAnthropic([
        response('{"alerts":[{"ref":"n1","title":"ЦБ сохранил ставку","why":"сигнал рынку"}]}', model="claude-haiku-5-5"),
    ])
    ctx = make_ctx(tmp_path, _handler, llm=fake)
    sent = []
    ctx.telegram = type("T", (), {"send": lambda self, t: sent.append(t)})()
    items = run_alerts(ctx)
    assert len(items) == 2 and sent[0].startswith("🚨 <b>Срочно</b>")
    assert "Индекс Мосбиржи −3,10%" in sent[0] and "ЦБ сохранил ставку" in sent[0]
    assert 'href="https://news.google.com/rss/articles/abc"' in sent[0]
    # первая проверка: только последний час → вчерашняя новость про ЦФА не ушла модели
    user = fake.calls[0]["messages"][0]["content"]
    assert "ЦБ сохранил" in user and "Сбер запустил" not in user and "Резкие движения" in user
    assert fake.calls[0]["model"] == "claude-haiku-5-5"
    assert fake.calls[0]["output_config"]["format"]["type"] == "json_schema"

    # повторная проверка: то же движение и те же новости → тишина, модель не вызывается
    later = NOW + timedelta(minutes=30)
    ctx.now = lambda: later
    assert run_alerts(ctx) == []
    assert len(fake.calls) == 1 and len(sent) == 1


def test_daily_limit(tmp_path):
    fake = FakeAnthropic([])
    ctx = make_ctx(tmp_path, _handler, llm=fake, dry_run=True)
    ctx.state["alerts_sent"] = {"2026-10-16": [{"at": "x", "text": "t"}] * 6}
    assert run_alerts(ctx) == []
    assert fake.calls == []


def test_bad_model_output_means_no_alert(tmp_path):
    def h(req):
        return httpx.Response(200, content=GN) if "news.google" in str(req.url) else httpx.Response(404)

    fake = FakeAnthropic([response("Срочного нет, всё спокойно.")])
    ctx = make_ctx(tmp_path, h, llm=fake, dry_run=True)
    assert run_alerts(ctx) == []
