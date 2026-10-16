"""Состояние бота в data/state.json с атомарной записью."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SEEN_LIMIT = 3000
ALERT_DAYS_KEPT = 14

DEFAULTS: dict[str, Any] = {
    "last_digest_at": {},          # kind -> iso (любая сводка, включая /digest)
    "last_scheduled_digest_at": None,  # iso: граница окна новостей
    "last_digest_text": "",
    "last_digest_kind": "",
    "tg_offset": 0,
    "seen_news": [],
    "seen_mail": [],
    "last_alert_check": None,
    "alerts_sent": {},             # "YYYY-MM-DD" -> [{"at", "text"}]
    "market_levels": {},           # "YYYY-MM-DD" -> {key: level}
    "chat_history": [],            # [{"q", "a"}]
}


class State:
    def __init__(self, path: Path | str, data: dict | None = None, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        self.lock = threading.RLock()
        self.data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        if data:
            self.data.update(data)

    @classmethod
    def load(cls, path: Path | str, readonly: bool = False) -> "State":
        path = Path(path)
        data = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                log.error("state.json повреждён (%s), начинаю с чистого состояния", e)
                broken = path.with_suffix(".broken.json")
                try:
                    os.replace(path, broken)
                except OSError:
                    pass
        return cls(path, data, readonly)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self.lock:
            self.data[key] = value

    def save(self) -> None:
        if self.readonly:
            return
        with self.lock:
            self._prune()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def _prune(self) -> None:
        for key in ("seen_news", "seen_mail"):
            if len(self.data[key]) > SEEN_LIMIT:
                self.data[key] = self.data[key][-SEEN_LIMIT:]
        for key in ("alerts_sent", "market_levels"):
            days = sorted(self.data[key])
            for d in days[:-ALERT_DAYS_KEPT]:
                del self.data[key][d]

    # --- просмотренные id ---
    def seen(self, kind: str) -> set[str]:
        with self.lock:
            return set(self.data[f"seen_{kind}"])

    def mark_seen(self, kind: str, ids) -> None:
        with self.lock:
            key = f"seen_{kind}"
            have = set(self.data[key])
            for i in ids:
                if i not in have:
                    self.data[key].append(i)
                    have.add(i)
            if len(self.data[key]) > SEEN_LIMIT:
                self.data[key] = self.data[key][-SEEN_LIMIT:]

    def alerts_today(self, day: str) -> list[dict]:
        with self.lock:
            return list(self.data["alerts_sent"].get(day, []))
