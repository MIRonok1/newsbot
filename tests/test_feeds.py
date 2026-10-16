from datetime import timedelta

import httpx

from newsbot.config import Feed
from newsbot.feeds import collect_news, normalize_title, parse_feed
from tests.conftest import FIXTURES, NOW, make_ctx

RBC = (FIXTURES / "rbc.xml").read_bytes()
GN = (FIXTURES / "gnews.xml").read_bytes()


def test_parse_window_and_cleanup():
    feed = Feed(name="РБК", url="u", topic="russia")
    items = parse_feed(RBC, feed, NOW - timedelta(hours=12))
    assert [i.title for i in items] == ["ЦБ сохранил ключевую ставку на уровне 16%"]
    assert items[0].summary == "Банк России сохранил ставку & дал сигнал."
    assert items[0].published.hour == 4


def test_no_window_keeps_undated():
    items = parse_feed(RBC, Feed(name="РБК", url="u", topic="russia"), None)
    assert len(items) == 3


def test_description_truncated():
    xml = RBC.replace(b"Banks", b"").replace(
        "Банк России <b>сохранил</b> ставку &amp; дал сигнал.".encode(), ("слово " * 100).encode())
    items = parse_feed(xml, Feed(name="РБК", url="u", topic="russia"), None, desc_chars=50)
    assert len(items[0].summary) <= 51 and items[0].summary.endswith("…")


def test_google_news_tail():
    feed = Feed(name="ЦФА", url="u", topic="cfa", google_news=True)
    items = parse_feed(GN, feed, NOW - timedelta(hours=48))
    assert items[0].source == "Интерфакс"
    assert items[0].title == "ЦБ сохранил ключевую ставку на уровне 16%"
    assert items[0].summary == ""
    assert items[1].source == "Ведомости"


def test_normalize():
    assert normalize_title("ЦБ  сохранил ставку! - РБК") == normalize_title("цб сохранил ставку")
    assert normalize_title("Ёлка") == "елка"


def test_collect_parallel_dedupe_and_failure(tmp_path):
    def handler(req: httpx.Request):
        if "rbc" in req.url.host:
            return httpx.Response(200, content=RBC)
        if "google" in req.url.host:
            return httpx.Response(200, content=GN)
        if "kommersant" in req.url.host:
            raise httpx.ConnectTimeout("boom")
        if "vedomosti" in req.url.host:
            return httpx.Response(200, content=b"<html>not a feed</html>")
        return httpx.Response(403)

    ctx = make_ctx(tmp_path, handler)
    items, failed = collect_news(ctx, 12)
    titles = [i.title for i in items]
    # Дубль «ЦБ сохранил…» из РБК и Google News остался один
    assert titles.count("ЦБ сохранил ключевую ставку на уровне 16%") == 1
    # ЦФА-лента с window_hours 48 захватывает вчерашнюю новость
    assert "Сбер запустил выпуск ЦФА на золото" in titles
    failed_names = dict(failed)
    assert failed_names["Коммерсантъ"] == "таймаут"
    assert "RSS" in failed_names["Ведомости"]
    assert failed_names["Axios"] == "HTTP 403"
    assert "РБК" not in failed_names
