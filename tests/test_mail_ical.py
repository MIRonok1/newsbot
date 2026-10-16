import base64
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from newsbot.config import Secrets, build_config
from newsbot.ical import events_for_day
from newsbot.mail import fetch_mail, parse_message
from tests.conftest import NOW, base_raw

MSK = ZoneInfo("Europe/Moscow")

MULTIPART = b"""From: =?UTF-8?B?0JjQstCw0L0g0J/QtdGC0YDQvtCy?= <ivan@example.com>
To: me@example.com
Subject: =?UTF-8?Q?=D0=94=D0=B5=D0=B4=D0=BB=D0=B0=D0=B9=D0=BD_=D0=BF=D0=BE_=D1=81=D1=82=D0=B0=D1=82=D1=8C=D0=B5?=
Date: Fri, 16 Oct 2026 07:15:00 +0300
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="XYZ"

--XYZ
Content-Type: text/plain; charset="utf-8"
Content-Transfer-Encoding: base64

""" + base64.encodebytes("Пришлите, пожалуйста, статью до пятницы.".encode()) + b"""
--XYZ
Content-Type: text/html; charset="utf-8"

<p>HTML version</p>
--XYZ--
"""

HTML_ONLY = """From: news@bank.ru
Subject: =?koi8-r?B?8NLJ18XU?=
Date: Fri, 16 Oct 2026 06:00:00 +0000
Content-Type: text/html; charset="windows-1251"
Content-Transfer-Encoding: 8bit

<html><body><style>p{color:red}</style><p>Ваш&nbsp;отчёт</p><br>готов</body></html>
""".encode("cp1251")


def test_multipart_base64_encoded_headers():
    sender, subject, dt, snippet = parse_message(MULTIPART)
    assert sender == "Иван Петров <ivan@example.com>"
    assert subject == "Дедлайн по статье"
    assert dt.astimezone(timezone.utc).hour == 4
    assert snippet == "Пришлите, пожалуйста, статью до пятницы."


def test_html_only_cp1251():
    sender, subject, _, snippet = parse_message(HTML_ONLY)
    assert sender == "news@bank.ru"
    assert subject == "Привет"
    assert snippet == "Ваш отчёт готов"


class FakeImap:
    def __init__(self, host):
        self.messages = {b"1": MULTIPART, b"2": HTML_ONLY}
        self.sizes = {b"1": 2000, b"2": 900_000}
        self.fetched = []
        self.readonly = None
        FakeImap.last = self

    def login(self, u, p):
        return "OK", [b""]

    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [b"2"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            assert "UNSEEN" in args and "SINCE" in args
            return "OK", [b"1 2"]
        uid, what = args
        self.fetched.append((uid, what))
        if what == "(RFC822.SIZE)":
            return "OK", [b"%s (UID %s RFC822.SIZE %d)" % (uid, uid, self.sizes[uid])]
        raw = self.messages[uid]
        if "HEADER" in what:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n"
        return "OK", [(b"%s (UID %s BODY[] {%d}" % (uid, uid, len(raw)), raw), b")"]

    def logout(self):
        pass


def test_fetch_mail_readonly_and_size_limit():
    cfg = build_config(base_raw(), Secrets(imap_host="h", imap_user="u", imap_password="p"))
    items = fetch_mail(cfg, NOW - timedelta(hours=12), imap_factory=FakeImap)
    assert FakeImap.last.readonly is True
    assert (b"2", "(BODY.PEEK[HEADER])") in FakeImap.last.fetched
    assert (b"1", "(BODY.PEEK[])") in FakeImap.last.fetched
    by_subj = {m.subject: m for m in items}
    assert "статью до пятницы" in by_subj["Дедлайн по статье"].snippet
    assert "большое письмо" in by_subj["Привет"].snippet


def test_fetch_mail_disabled():
    assert fetch_mail(build_config(base_raw()), NOW) == []


ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:1
SUMMARY:Seminar on CFA
DTSTART;TZID=Europe/Moscow:20261002T100000
DTEND;TZID=Europe/Moscow:20261002T113000
RRULE:FREQ=WEEKLY;BYDAY=FR
LOCATION:Room 5
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261016
DTEND;VALUE=DATE:20261017
END:VEVENT
BEGIN:VEVENT
UID:3
SUMMARY:Other day
DTSTART:20261017T090000Z
DTEND:20261017T100000Z
END:VEVENT
END:VCALENDAR
"""


def test_recurring_event_expanded():
    evs = events_for_day(ICS, date(2026, 10, 16), MSK)
    assert [e.summary for e in evs] == ["Holiday", "Seminar on CFA"]
    assert evs[0].all_day and evs[0].line(MSK).startswith("весь день")
    assert evs[1].line(MSK) == "10:00 — Seminar on CFA (Room 5)"
    assert events_for_day(ICS, date(2026, 10, 18), MSK) == []
