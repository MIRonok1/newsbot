"""Мелкие помощники для текста, дат и чисел."""
from __future__ import annotations

import html
import re
from datetime import date, datetime

_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"<\s*(br|/p|/div|/li|/tr|/h\d)\b[^>]*>", re.I)
_DROP = re.compile(r"<(script|style|head)\b.*?</\1\s*>", re.I | re.S)
_WS = re.compile(r"[ \t\r\f\v ]+")

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


def html_to_text(raw: str, keep_lines: bool = False) -> str:
    """HTML → обычный текст: без тегов, с раскрытыми сущностями."""
    if not raw:
        return ""
    text = _DROP.sub(" ", raw)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    if keep_lines:
        lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
        out, blank = [], False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank and out:
                out.append("")
                blank = True
        return "\n".join(out).strip()
    return _WS.sub(" ", text.replace("\n", " ")).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",.;:—- ")
    return (cut or text[:limit]) + "…"


def ru_date(d: date | datetime) -> str:
    """пт, 17 октября"""
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def fmt_num(value: float, digits: int = 2) -> str:
    s = f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")
    return s


def fmt_pct(pct: float) -> str:
    sign = "+" if pct > 0 else ("−" if pct < 0 else "")
    return f"{sign}{fmt_num(abs(pct), 2)}%"
