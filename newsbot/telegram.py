"""Telegram Bot API напрямую через httpx."""
from __future__ import annotations

import logging
import time

import httpx

from .sanitize import split_message, to_plain

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    def __init__(self, description: str, code: int | None = None):
        super().__init__(description)
        self.description = description
        self.code = code


class Telegram:
    def __init__(self, http: httpx.Client, token: str, chat_id: str = "", timeout: float = 20,
                 sleep=time.sleep):
        self.http = http
        self.token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self.sleep = sleep

    def call(self, method: str, payload: dict | None = None, timeout: float | None = None,
             retries: int = 3):
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN не задан")
        url = API.format(token=self.token, method=method)
        for attempt in range(retries + 1):
            try:
                resp = self.http.post(url, json=payload or {}, timeout=timeout or self.timeout)
            except httpx.HTTPError as e:
                # str(e) может содержать URL с токеном — наружу только тип ошибки
                if attempt < retries:
                    self.sleep(min(2 ** attempt, 10))
                    continue
                raise TelegramError(f"сеть: {type(e).__name__}") from None
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": False, "description": f"HTTP {resp.status_code}"}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", resp.status_code)
            desc = data.get("description", f"HTTP {resp.status_code}")
            if code == 429 and attempt < retries:
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                log.warning("Telegram 429, жду %s с", wait)
                self.sleep(float(wait) + 0.5)
                continue
            if code >= 500 and attempt < retries:
                self.sleep(min(2 ** attempt, 10))
                continue
            raise TelegramError(desc, code)
        raise TelegramError("исчерпаны попытки")

    def send(self, text: str, chat_id: str | int | None = None, html: bool = True) -> int:
        """Отправляет текст, деля на части по 4096. Возвращает число отправленных частей."""
        chat = chat_id or self.chat_id
        if not chat:
            raise TelegramError("TELEGRAM_CHAT_ID не задан")
        parts = split_message(text) if html else _split_plain(text)
        sent = 0
        for part in parts:
            payload = {"chat_id": chat, "text": part, "link_preview_options": {"is_disabled": True}}
            if html:
                payload["parse_mode"] = "HTML"
            try:
                self.call("sendMessage", payload)
            except TelegramError as e:
                if html and e.code == 400 and "parse" in e.description.lower():
                    log.warning("Telegram не разобрал HTML (%s), отправляю без разметки", e.description)
                    plain = dict(payload, text=to_plain(part))
                    plain.pop("parse_mode")
                    self.call("sendMessage", plain)
                else:
                    raise
            sent += 1
        return sent

    def typing(self, chat_id: str | int | None = None) -> None:
        try:
            self.call("sendChatAction", {"chat_id": chat_id or self.chat_id, "action": "typing"}, retries=0)
        except TelegramError:
            pass

    def get_updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return self.call("getUpdates", {"offset": offset, "timeout": timeout,
                                        "allowed_updates": ["message"]},
                         timeout=timeout + 15, retries=0) or []

    def get_me(self) -> dict:
        return self.call("getMe")


def _split_plain(text: str, limit: int = 4000) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or [""]

