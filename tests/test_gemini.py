import json

import httpx
import pytest

from newsbot import gemini as gemini_mod
from newsbot import llm
from newsbot.checks import run_checks
from newsbot.gemini import GeminiClient
from newsbot.usage import month_usage
from tests.conftest import NOW, make_ctx


def _reply(text="ответ", finish="STOP", thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 200, "thoughtsTokenCount": 50},
            "modelVersion": "gemini-3.8-flash"}


def _client(script, calls):
    def handler(req):
        calls.append(req)
        item = script.pop(0)
        if isinstance(item, int):
            return httpx.Response(item, json={"error": {"message": f"err {item}"}})
        return httpx.Response(200, json=item)
    return GeminiClient("KEY", http=httpx.Client(transport=httpx.MockTransport(handler)))


def _ask(ctx, **kw):
    params = dict(purpose="t", model="gemini-3.8-flash", system="sys",
                  messages=[{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                            {"role": "user", "content": "q2"}],
                  max_tokens=100, effort="low", search_uses=3)
    params.update(kw)
    return llm.ask(ctx, **params)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gemini_mod, "_sleep", lambda s: None)


def test_gemini_request_shape_and_usage(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, gemini=_client([_reply("итог", thought="думаю")], calls))
    res = _ask(ctx)
    assert res.text == "итог" and res.stop_reason == "end_turn" and res.usd == 0
    req = calls[0]
    assert req.url.path.endswith("/models/gemini-3.8-flash:generateContent")
    assert req.headers["x-goog-api-key"] == "KEY" and "KEY" not in str(req.url)
    body = json.loads(req.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "sys"
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["generationConfig"] == {"maxOutputTokens": 100, "thinkingConfig": {"thinkingLevel": "low"}}
    assert "tools" not in body  # у бесплатной Gemini поиска нет
    m = month_usage(ctx.cfg.data_dir / "usage.jsonl", NOW)
    assert m.calls == 1 and m.input_tokens == 1000 and m.output_tokens == 250


def test_gemini_structured_output(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, gemini=_client([_reply('{"alerts": []}')], calls))
    _ask(ctx, output_format={"type": "json_schema", "schema": {"type": "object"}})
    gen = json.loads(calls[0].content)["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"] == {"type": "object"}


def test_gemini_retries_without_thinking_on_400(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, gemini=_client([400, _reply("ok")], calls))
    assert _ask(ctx).text == "ok"
    assert "thinkingConfig" not in json.loads(calls[1].content)["generationConfig"]


def test_gemini_rate_limit_retries_then_fails(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, gemini=_client([429, 429, 429], calls))
    with pytest.raises(llm.LLMError, match="лимит бесплатного тарифа"):
        _ask(ctx)
    assert len(calls) == 3


def test_gemini_rate_limit_recovers(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, gemini=_client([503, _reply("ok")], calls))
    assert _ask(ctx).text == "ok"


def test_gemini_safety_and_max_tokens(tmp_path):
    ctx = make_ctx(tmp_path, gemini=_client([_reply("", finish="SAFETY")], []))
    with pytest.raises(llm.LLMError, match="отказалась"):
        _ask(ctx)
    ctx = make_ctx(tmp_path, gemini=_client([_reply("обрыв", finish="MAX_TOKENS")], []))
    assert _ask(ctx).stop_reason == "max_tokens"


def test_gemini_without_key(tmp_path):
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY"):
        _ask(make_ctx(tmp_path))


def _gemini_raw():
    from tests.conftest import base_raw
    raw = base_raw()
    raw["models"].update(digest="gemini-3.8-flash", chat="gemini-3.8-flash", alerts="gemini-3.5-flash-lite")
    raw["feeds"] = []
    raw["markets"] = []
    return raw


def test_checks_gemini_models(tmp_path):
    calls = []
    ctx = make_ctx(tmp_path, raw=_gemini_raw(), gemini=_client([{"name": "m"}, 404], calls))
    rows = {name: (ok, detail) for name, ok, detail in run_checks(ctx, telegram=False)}
    assert rows["Gemini API: gemini-3.8-flash"] == (True, "доступна")
    assert rows["Gemini API: gemini-3.5-flash-lite"][0] is False
    assert len(calls) == 2  # digest и chat — одна модель, проверяется один раз


def test_checks_gemini_without_key(tmp_path):
    rows = run_checks(make_ctx(tmp_path, raw=_gemini_raw()), telegram=False)
    assert ("Gemini API", False, "GEMINI_API_KEY не задан") in rows


def test_digest_with_gemini_has_no_search(tmp_path):
    from newsbot import digest

    calls = []
    ctx = make_ctx(tmp_path, raw=_gemini_raw(), dry_run=True,
                   gemini=_client([_reply("<digest>• <b>Суть.</b> Детали.</digest>")], calls))
    res = digest.generate(ctx, digest.collect(ctx, "morning"))
    assert not res.fallback and "Суть" in res.text
    system = json.loads(calls[0].content)["systemInstruction"]["parts"][0]["text"]
    assert "поиск недоступен" in system
