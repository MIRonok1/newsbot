"""Минимальный клиент Google Gemini API (REST generateContent) на httpx."""
from __future__ import annotations

import time

import httpx

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
RETRY_STATUSES = (429, 500, 503)
RETRY_DELAYS = (10, 30)


class GeminiError(Exception):
    def __init__(self, status_code: int | None, message: str):
        super().__init__(f"{status_code}: {message}" if status_code else message)
        self.status_code = status_code
        self.message = message


def _sleep(seconds: float) -> None:  # подменяется в тестах
    time.sleep(seconds)


class GeminiClient:
    def __init__(self, api_key: str, http: httpx.Client | None = None, timeout: float = 300.0):
        self.http = http or httpx.Client(timeout=timeout)
        self.headers = {"x-goog-api-key": api_key}  # ключ в заголовке, а не в URL — не попадёт в логи

    def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                resp = self.http.request(method, f"{BASE_URL}/{path}", headers=self.headers, json=json)
            except httpx.HTTPError as e:
                raise GeminiError(None, f"сеть: {e}") from e
            if resp.status_code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                _sleep(RETRY_DELAYS[attempt])
                continue
            if resp.status_code >= 400:
                try:
                    msg = resp.json().get("error", {}).get("message") or resp.text
                except ValueError:
                    msg = resp.text
                raise GeminiError(resp.status_code, msg[:500])
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def generate(self, model: str, body: dict) -> dict:
        return self._request("POST", f"models/{model}:generateContent", body)

    def get_model(self, model: str) -> dict:
        return self._request("GET", f"models/{model}")


def make_gemini(api_key: str):
    return GeminiClient(api_key) if api_key else None
