"""Почта по IMAP, только чтение: непрочитанные письма за окно."""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime

from .textutil import html_to_text, truncate

log = logging.getLogger(__name__)
IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass
class MailItem:
    id: str
    sender: str
    subject: str
    date: datetime | None
    snippet: str
    ref: str = ""


def _decode_header(value) -> str:
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value)))).strip()
    except Exception:
        return str(value).strip()


def _part_text(part: Message) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")


def _body_text(msg: Message) -> str:
    try:
        body = msg.get_body(preferencelist=("plain", "html"))
    except Exception:
        body = None
    if body is None:
        for part in msg.walk():
            if part.get_content_maintype() == "text":
                body = part
                break
    if body is None:
        return ""
    text = _part_text(body)
    if body.get_content_subtype() == "html":
        text = html_to_text(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_message(raw: bytes, snippet_chars: int = 400) -> tuple[str, str, datetime | None, str]:
    """(отправитель, тема, дата, сниппет) из сырого письма."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    name, addr = parseaddr(_decode_header(msg.get("From")))
    sender = f"{name} <{addr}>" if name and addr else (addr or name)
    subject = _decode_header(msg.get("Subject")) or "(без темы)"
    date = None
    try:
        if msg.get("Date"):
            date = parsedate_to_datetime(str(msg.get("Date")))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        date = None
    body = ""
    if msg.get_content_maintype() in ("text", "multipart"):
        body = _body_text(msg)
    return sender, subject, date, truncate(body, snippet_chars)


def _imap_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def _size(fetch_data) -> int:
    for item in fetch_data or []:
        raw = item[0] if isinstance(item, tuple) else item
        if isinstance(raw, bytes):
            m = re.search(rb"RFC822\.SIZE (\d+)", raw)
            if m:
                return int(m.group(1))
    return 0


def _payload(fetch_data) -> bytes:
    for item in fetch_data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
            return item[1]
    return b""


def fetch_mail(cfg, since: datetime, imap_factory=None) -> list[MailItem]:
    s = cfg.secrets
    if not s.mail_enabled:
        return []
    mcfg = cfg.section("mail")
    limit = int(mcfg.get("max_size_kb", 300)) * 1024
    snippet_chars = int(mcfg.get("snippet_chars", 400))
    max_messages = int(mcfg.get("max_messages", 30))
    factory = imap_factory or (lambda host: imaplib.IMAP4_SSL(host, timeout=30))
    conn = factory(s.imap_host)
    items: list[MailItem] = []
    try:
        conn.login(s.imap_user, s.imap_password)
        typ, _ = conn.select(mcfg.get("mailbox", "INBOX"), readonly=True)
        if typ != "OK":
            raise RuntimeError("не удалось открыть папку")
        typ, data = conn.uid("SEARCH", None, "SINCE", _imap_date(since), "UNSEEN")
        if typ != "OK":
            raise RuntimeError("поиск писем не удался")
        uids = (data[0] or b"").split()[-max_messages:]
        for uid in uids:
            typ, size_data = conn.uid("FETCH", uid, "(RFC822.SIZE)")
            size = _size(size_data)
            what = "(BODY.PEEK[])" if size and size < limit else "(BODY.PEEK[HEADER])"
            typ, data = conn.uid("FETCH", uid, what)
            raw = _payload(data)
            if not raw:
                continue
            sender, subject, date, snippet = parse_message(raw, snippet_chars)
            if date and date < since:
                continue
            if what.endswith("HEADER])"):
                snippet = f"(большое письмо, {size // 1024} КБ — текст не загружался)"
            uid_s = uid.decode() if isinstance(uid, bytes) else str(uid)
            mid = hashlib.sha1(f"{s.imap_user}:{uid_s}:{subject}".encode()).hexdigest()[:16]
            items.append(MailItem(mid, sender, subject, date, snippet))
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    items.sort(key=lambda m: m.date or since, reverse=True)
    return items


def mail_for_prompt(items: list[MailItem], send_body: bool, tz) -> str:
    lines = []
    for m in items:
        when = m.date.astimezone(tz).strftime("%d.%m %H:%M") if m.date else "?"
        line = f"[{m.ref}] {when} · {m.sender} · «{m.subject}»"
        if send_body and m.snippet:
            line += f"\n    {m.snippet}"
        lines.append(line)
    return "\n".join(lines)
