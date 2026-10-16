"""Котировки: Мосбиржа ISS, курсы ЦБ (JSON, запасной XML), Yahoo Finance."""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Market
from .textutil import fmt_num, fmt_pct

log = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")

ISS_INDEX = ("https://iss.moex.com/iss/engines/stock/markets/index/securities/{secid}.json"
             "?iss.meta=off&iss.only=marketdata,securities")
ISS_CURRENCY = ("https://iss.moex.com/iss/engines/currency/markets/selt/boards/CETS/securities/"
                "{secid}.json?iss.meta=off&iss.only=marketdata,securities")
CBR_JSON = "https://www.cbr-xml-daily.ru/daily_json.js"
CBR_XML = "https://www.cbr.ru/scripts/XML_daily.asp"
YAHOO = "https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
_yahoo_lock = threading.Lock()  # Yahoo режет параллельные запросы (429) — ходим по одному


@dataclass
class Quote:
    key: str
    name: str
    value: float | None = None
    change_pct: float | None = None
    as_of: datetime | None = None
    alert_pct: float | None = None
    error: str | None = None

    def line(self, tz=MSK) -> str:
        if self.value is None:
            return f"{self.name}: нет данных ({self.error or '—'})"
        s = f"{self.name}: {fmt_num(self.value)}"
        if self.change_pct is not None:
            s += f" ({fmt_pct(self.change_pct)} за день)"
        if self.as_of:
            local = self.as_of.astimezone(tz)
            s += f", данные на {local:%d.%m %H:%M} МСК"
        return s


# --- парсеры ----------------------------------------------------------------------

def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rows(block: dict) -> list[dict]:
    cols = block.get("columns") or []
    return [dict(zip(cols, row)) for row in block.get("data") or []]


def parse_iss(data: dict, secid: str) -> tuple[float | None, float | None, datetime | None]:
    """Значение, изменение % и время из ответа ISS. Колонки читаются защитно."""
    rows = [r for r in _rows(data.get("marketdata") or {}) if r.get("SECID", secid) == secid]
    if not rows:
        raise ValueError(f"нет marketdata для {secid}")
    r = rows[0]
    value = next((v for v in (_num(r.get(k)) for k in ("CURRENTVALUE", "LAST", "LCURRENTPRICE"))
                  if v is not None), None)
    change = next((v for v in (_num(r.get(k)) for k in ("LASTCHANGEPRC", "LASTTOPREVPRICE"))
                   if v is not None), None)
    as_of = None
    raw_time = r.get("SYSTIME") or r.get("UPDATETIME")
    if raw_time:
        try:
            as_of = datetime.strptime(str(raw_time)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
        except ValueError:
            as_of = None
    return value, change, as_of


def parse_cbr_json(data: dict) -> dict[str, tuple[float, float | None, datetime | None]]:
    as_of = None
    if data.get("Date"):
        try:
            as_of = datetime.fromisoformat(data["Date"])
        except ValueError:
            pass
    out = {}
    for code, v in (data.get("Valute") or {}).items():
        nominal = _num(v.get("Nominal")) or 1
        value = _num(v.get("Value"))
        prev = _num(v.get("Previous"))
        if value is None:
            continue
        pct = (value - prev) / prev * 100 if prev else None
        out[code] = (value / nominal, pct, as_of)
    return out


def parse_cbr_xml(content: bytes) -> dict[str, tuple[float, float | None, datetime | None]]:
    """XML_daily.asp: windows-1251, десятичные запятые, курс за Nominal единиц."""
    text = content.decode("cp1251", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    root = ET.fromstring(text)
    as_of = None
    if root.get("Date"):
        try:
            as_of = datetime.strptime(root.get("Date"), "%d.%m.%Y").replace(hour=11, minute=30, tzinfo=MSK)
        except ValueError:
            pass
    out = {}
    for v in root.findall("Valute"):
        code = (v.findtext("CharCode") or "").strip()
        value = _num((v.findtext("Value") or "").replace(",", "."))
        nominal = _num((v.findtext("Nominal") or "1").replace(",", ".")) or 1
        if code and value is not None:
            out[code] = (value / nominal, None, as_of)
    return out


def parse_yahoo(data: dict) -> tuple[float | None, float | None, datetime | None]:
    result = ((data.get("chart") or {}).get("result") or [None])[0]
    if not result:
        err = (data.get("chart") or {}).get("error") or {}
        raise ValueError(err.get("description") or "пустой ответ Yahoo")
    meta = result.get("meta") or {}
    price = _num(meta.get("regularMarketPrice"))
    closes = []
    try:
        closes = result["indicators"]["quote"][0]["close"] or []
    except (KeyError, IndexError, TypeError):
        pass
    # Последний бар — текущая сессия, предыдущее закрытие — последний непустой бар до него
    prev = None
    for c in reversed(closes[:-1]):
        if c is not None:
            prev = float(c)
            break
    if prev is None:
        prev = _num(meta.get("previousClose")) or _num(meta.get("chartPreviousClose"))
    if price is None and closes and closes[-1] is not None:
        price = float(closes[-1])
    pct = (price - prev) / prev * 100 if price is not None and prev else None
    ts = meta.get("regularMarketTime")
    as_of = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) else None
    return price, pct, as_of


# --- загрузка ----------------------------------------------------------------------

def _get_json(http, url: str, timeout: float, headers: dict | None = None) -> dict:
    resp = http.get(url, timeout=timeout, headers=headers)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    return resp.json()


def fetch_yahoo(http, symbol: str, timeout: float, pause: float = 1.5) -> dict:
    with _yahoo_lock:
        last = None
        for i, host in enumerate(("query1", "query2", "query1")):
            if i:
                time.sleep(pause * i)
            try:
                # С полным браузерным User-Agent Yahoo отвечает 429, с коротким — 200
                return _get_json(http, YAHOO.format(host=host, symbol=symbol), timeout,
                                 headers={"User-Agent": "Mozilla/5.0"})
            except ValueError as e:
                last = e
                if "429" not in str(e) and "HTTP 5" not in str(e):
                    raise
        raise last


def fetch_cbr(http, timeout: float) -> dict:
    try:
        return parse_cbr_json(_get_json(http, CBR_JSON, timeout))
    except Exception as e:
        log.warning("cbr-xml-daily недоступен (%s), пробую XML ЦБ", e)
    resp = http.get(CBR_XML, timeout=timeout)
    if resp.status_code != 200:
        raise ValueError(f"ЦБ: HTTP {resp.status_code}")
    return parse_cbr_xml(resp.content)


def fetch_one(http, m: Market, timeout: float, cbr_cache: dict) -> Quote:
    q = Quote(key=m.key, name=m.name, alert_pct=m.alert_pct)
    try:
        if m.source == "moex_index":
            q.value, q.change_pct, q.as_of = parse_iss(_get_json(http, ISS_INDEX.format(secid=m.secid), timeout), m.secid)
        elif m.source == "moex_currency":
            q.value, q.change_pct, q.as_of = parse_iss(_get_json(http, ISS_CURRENCY.format(secid=m.secid), timeout), m.secid)
        elif m.source == "yahoo":
            q.value, q.change_pct, q.as_of = parse_yahoo(fetch_yahoo(http, m.symbol, timeout))
        elif m.source == "cbr":
            if "data" not in cbr_cache:
                cbr_cache["data"] = fetch_cbr(http, timeout)
            rates = cbr_cache["data"]
            if m.code not in rates:
                raise ValueError(f"нет курса {m.code}")
            q.value, q.change_pct, q.as_of = rates[m.code]
        if q.value is None:
            q.error = "нет значения"
    except Exception as e:
        q.error = str(e) or type(e).__name__
        log.warning("котировка %s: %s", m.key, q.error)
    return q


def fetch_quotes(ctx) -> list[Quote]:
    timeout = ctx.cfg.get("http.timeout", 15)
    markets = ctx.cfg.markets
    cbr_cache: dict = {}
    # ЦБ грузим заранее одним запросом, чтобы USD и EUR не дублировали его
    if any(m.source == "cbr" for m in markets):
        try:
            cbr_cache["data"] = fetch_cbr(ctx.http, timeout)
        except Exception as e:
            cbr_cache["data"] = {}
            log.warning("курсы ЦБ: %s", e)
    with ThreadPoolExecutor(max_workers=ctx.cfg.get("http.workers", 8)) as pool:
        return list(pool.map(lambda m: fetch_one(ctx.http, m, timeout, cbr_cache), markets))
