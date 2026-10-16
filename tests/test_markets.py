import json
from datetime import datetime, timezone

import httpx

from newsbot.markets import fetch_quotes, parse_cbr_json, parse_cbr_xml, parse_iss, parse_yahoo
from tests.conftest import make_ctx

ISS_INDEX = {
    "securities": {"columns": ["SECID", "NAME"], "data": [["IMOEX", "Индекс МосБиржи"]]},
    "marketdata": {"columns": ["SECID", "CURRENTVALUE", "LASTVALUE", "LASTCHANGEPRC", "SYSTIME"],
                   "data": [["IMOEX", 2845.31, 2810.0, 1.25, "2026-10-15 18:50:12"]]},
}
ISS_CNY = {
    "securities": {"columns": ["SECID"], "data": [["CNYRUB_TOM"]]},
    "marketdata": {"columns": ["SECID", "BOARDID", "LAST", "LASTTOPREVPRICE", "SYSTIME"],
                   "data": [["CNYRUB_TOM", "CETS", 11.2, -2.4, "2026-10-16 07:40:00"]]},
}
CBR_JSON = {
    "Date": "2026-10-16T11:30:00+03:00",
    "Valute": {
        "USD": {"CharCode": "USD", "Nominal": 1, "Value": 82.0, "Previous": 80.0},
        "EUR": {"CharCode": "EUR", "Nominal": 1, "Value": 90.0, "Previous": 90.0},
        "AMD": {"CharCode": "AMD", "Nominal": 100, "Value": 21.0, "Previous": 21.0},
    },
}
CBR_XML = ('<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="16.10.2026" name="Foreign Currency Market">'
           '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>'
           '<Name>Доллар США</Name><Value>81,2345</Value></Valute>'
           '<Valute ID="R01060"><NumCode>051</NumCode><CharCode>AMD</CharCode><Nominal>100</Nominal>'
           '<Name>Армянских драмов</Name><Value>21,5000</Value></Valute></ValCurs>').encode("cp1251")
YAHOO = {"chart": {"result": [{
    "meta": {"regularMarketPrice": 104.0, "chartPreviousClose": 50.0, "regularMarketTime": 1792130400},
    "timestamp": [1, 2, 3, 4],
    "indicators": {"quote": [{"close": [98.0, 100.0, None, 103.5]}]},
}], "error": None}}


def test_iss_index():
    v, pct, at = parse_iss(ISS_INDEX, "IMOEX")
    assert v == 2845.31 and pct == 1.25
    assert at.hour == 18 and at.utcoffset().total_seconds() == 3 * 3600


def test_iss_currency_fallback_columns():
    v, pct, _ = parse_iss(ISS_CNY, "CNYRUB_TOM")
    assert v == 11.2 and pct == -2.4


def test_iss_null_current_uses_last():
    data = {"marketdata": {"columns": ["SECID", "CURRENTVALUE", "LAST", "LASTCHANGEPRC", "LASTTOPREVPRICE"],
                           "data": [["X", None, 5.0, None, 0.7]]}}
    assert parse_iss(data, "X")[:2] == (5.0, 0.7)


def test_cbr_json():
    r = parse_cbr_json(CBR_JSON)
    assert r["USD"][0] == 82.0 and round(r["USD"][1], 2) == 2.5
    assert r["EUR"][1] == 0
    assert r["AMD"][0] == 0.21


def test_cbr_xml_cp1251():
    r = parse_cbr_xml(CBR_XML)
    assert r["USD"][0] == 81.2345 and r["USD"][1] is None
    assert r["AMD"][0] == 0.215
    assert r["USD"][2].day == 16


def test_yahoo_prev_close_not_chart_previous():
    price, pct, at = parse_yahoo(YAHOO)
    assert price == 104.0
    assert round(pct, 2) == 4.0  # против 100.0 (последний непустой бар до текущего)
    assert at.tzinfo is timezone.utc


def test_fetch_quotes_with_fallbacks(tmp_path):
    calls = []

    def handler(req: httpx.Request):
        calls.append(str(req.url))
        url = str(req.url)
        if "securities/IMOEX" in url:
            return httpx.Response(200, json=ISS_INDEX)
        if "CNYRUB_TOM" in url:
            return httpx.Response(200, json=ISS_CNY)
        if "cbr-xml-daily" in url:
            return httpx.Response(503)
        if "XML_daily" in url:
            return httpx.Response(200, content=CBR_XML)
        if "yahoo" in url:
            return httpx.Response(200, json=YAHOO)
        return httpx.Response(500)

    ctx = make_ctx(tmp_path, handler)
    quotes = {q.key: q for q in fetch_quotes(ctx)}
    assert quotes["IMOEX"].value == 2845.31
    assert quotes["RGBI"].error == "HTTP 500"
    assert quotes["USD"].value == 81.2345
    assert quotes["EUR"].error and "EUR" in quotes["EUR"].error
    assert quotes["BTC"].change_pct is not None
    assert sum("XML_daily" in c for c in calls) == 1
    assert "(+1,25% за день)" in quotes["IMOEX"].line()
