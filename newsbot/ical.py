"""Календарь по секретной iCal-ссылке, с разворачиванием повторов."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events


@dataclass
class Event:
    start: datetime | date
    all_day: bool
    summary: str
    location: str = ""

    def line(self, tz: ZoneInfo) -> str:
        when = "весь день" if self.all_day else self.start.astimezone(tz).strftime("%H:%M")
        s = f"{when} — {self.summary}"
        if self.location:
            s += f" ({self.location})"
        return s


def events_for_day(ics: bytes, day: date, tz: ZoneInfo) -> list[Event]:
    cal = icalendar.Calendar.from_ical(ics)
    start = datetime.combine(day, time.min, tz)
    end = start + timedelta(days=1)
    out = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        dt = ev.get("DTSTART").dt
        all_day = not isinstance(dt, datetime)
        if not all_day and dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        out.append(Event(
            start=dt, all_day=all_day,
            summary=str(ev.get("SUMMARY", "(без названия)")).strip(),
            location=str(ev.get("LOCATION", "") or "").strip(),
        ))
    out.sort(key=lambda e: (not e.all_day, e.start if not e.all_day else datetime.min.replace(tzinfo=tz)))
    return out


def fetch_events(ctx, day: date) -> list[Event]:
    url = ctx.cfg.secrets.ics_url
    if not url:
        return []
    resp = ctx.http.get(url, timeout=ctx.cfg.get("http.timeout", 15))
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    return events_for_day(resp.content, day, ctx.cfg.tz)
