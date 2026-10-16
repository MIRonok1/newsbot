"""Санитайзер Telegram HTML и деление длинных сообщений."""
from __future__ import annotations

import html
import re

ALLOWED = {"b", "i", "u", "s", "a", "code", "blockquote"}
ALIASES = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s"}
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)\s*/?>")
_ENTITY = re.compile(r"&(#\d{1,7}|#x[0-9a-fA-F]{1,6}|amp|lt|gt|quot);")
_HREF = re.compile(r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
TG_LIMIT = 4096


def _escape_text(text: str) -> str:
    """Экранирует <, > и & — кроме уже корректных сущностей."""
    out = []
    pos = 0
    for m in _ENTITY.finditer(text):
        out.append(html.escape(text[pos:m.start()], quote=False))
        out.append(m.group(0))
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    return "".join(out)


def _safe_href(attrs: str) -> str | None:
    m = _HREF.search(attrs or "")
    if not m:
        return None
    url = html.unescape(next(g for g in m.groups() if g is not None)).strip()
    if not re.match(r"^https?://[^\s]+$", url, re.I):
        return None
    return html.escape(url, quote=True)


def sanitize_html(text: str) -> str:
    """Белый список тегов, экранирование остального, балансировка, только http(s)-ссылки."""
    out: list[str] = []
    stack: list[str] = []
    pos = 0
    for m in _TAG.finditer(text):
        out.append(_escape_text(text[pos:m.start()]))
        pos = m.end()
        closing, name, attrs = m.group(1) == "/", m.group(2).lower(), m.group(3)
        if name == "br":
            out.append("\n")
            continue
        name = ALIASES.get(name, name)
        if name not in ALLOWED:
            if name in ("p", "div", "li", "ul", "ol", "span", "pre", "tg-spoiler"):
                if closing and name in ("p", "div", "li"):
                    out.append("\n")
                continue
            out.append(_escape_text(m.group(0)))
            continue
        if not closing:
            if name == "a":
                href = _safe_href(attrs)
                if href is None or "a" in stack:
                    stack.append("a!")  # пропущенная ссылка: текст остаётся, тег нет
                    continue
                out.append(f'<a href="{href}">')
            else:
                out.append(f"<{name}>")
            stack.append(name)
        else:
            if name == "a":
                links = [i for i, t in enumerate(stack) if t in ("a", "a!")]
                if links and stack[links[-1]] == "a!":
                    del stack[links[-1]]
                    continue
            if name not in stack:
                continue  # лишний закрывающий тег
            while stack:
                top = stack.pop()
                if top == "a!":
                    continue
                out.append(f"</{top}>")
                if top == name:
                    break
    out.append(_escape_text(text[pos:]))
    for top in reversed(stack):
        if top != "a!":
            out.append(f"</{top}>")
    result = "".join(out)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def to_plain(text: str) -> str:
    """Telegram HTML → обычный текст; ссылки превращаются в «текст (url)»."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', lambda m: f"{m.group(2)} ({html.unescape(m.group(1))})",
                  text, flags=re.S)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def _hard_split(block: str, limit: int) -> list[str]:
    parts = []
    while len(block) > limit:
        cut = block.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = block.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        # не резать посреди тега или сущности
        lt, gt = block.rfind("<", 0, cut), block.rfind(">", 0, cut)
        if lt > gt:
            cut = lt
        amp = block.rfind("&", 0, cut)
        if amp != -1 and ";" not in block[amp:cut]:
            cut = amp
        if cut <= 0:
            cut = limit
        parts.append(block[:cut])
        block = block[cut:].lstrip()
    if block:
        parts.append(block)
    return parts


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Делит по абзацам, затем по строкам; каждая часть — сбалансированный HTML."""
    budget = limit - 200  # запас на закрывающие теги после балансировки
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    cur = ""
    for para in paragraphs:
        pieces = [para] if len(para) <= budget else _hard_split(para, budget)
        for piece in pieces:
            candidate = f"{cur}\n\n{piece}" if cur else piece
            if len(candidate) <= budget:
                cur = candidate
            else:
                if cur:
                    chunks.append(cur)
                cur = piece
    if cur:
        chunks.append(cur)
    return [s for s in (sanitize_html(c) for c in chunks) if s]
