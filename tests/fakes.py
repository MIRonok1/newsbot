"""Фейковый клиент Anthropic для тестов."""
from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2


def text_block(text):
    return SimpleNamespace(type="text", text=text)


def response(text="", stop="end_turn", content=None, inp=1000, out=500, searches=0, model="claude-sonnet-5-5"):
    usage = SimpleNamespace(input_tokens=inp, output_tokens=out, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0,
                            server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=0))
    return SimpleNamespace(content=content if content is not None else [text_block(text)],
                           stop_reason=stop, usage=usage, model=model)


def bad_request(msg="tool not supported"):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.BadRequestError(msg, response=httpx2.Response(400, request=req), body=None)


class _Messages:
    def __init__(self, parent, beta):
        self.parent, self.beta = parent, beta

    def create(self, **kwargs):
        kwargs["_beta"] = self.beta
        self.parent.calls.append(kwargs)
        item = self.parent.script.pop(0) if self.parent.script else response("")
        if callable(item):
            item = item(kwargs)
        if isinstance(item, Exception):
            raise item
        return item


class FakeAnthropic:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.calls: list[dict] = []
        self.messages = _Messages(self, False)
        self.beta = SimpleNamespace(messages=_Messages(self, True))
        self.models = SimpleNamespace(retrieve=lambda model: SimpleNamespace(id=model))
