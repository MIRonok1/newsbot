import json

from newsbot import llm
from newsbot.usage import budget_mode, month_usage
from tests.conftest import NOW, make_ctx
from tests.fakes import FakeAnthropic, bad_request, response, text_block


def _ask(ctx, **kw):
    params = dict(purpose="t", model="claude-sonnet-5-5", system="s",
                  messages=[{"role": "user", "content": "q"}], max_tokens=100, effort="low", search_uses=3)
    params.update(kw)
    return llm.ask(ctx, **params)


def test_pause_turn_continues_with_assistant_content(tmp_path):
    first = response(content=[text_block("Ищу… "), {"type": "server_tool_use", "id": "x"}], stop="pause_turn", searches=1)
    fake = FakeAnthropic([first, response("<digest>итог</digest>", searches=2)])
    ctx = make_ctx(tmp_path, llm=fake)
    res = _ask(ctx)
    assert res.text == "Ищу… <digest>итог</digest>"
    assert res.searches == 3
    second = fake.calls[1]["messages"]
    assert second[-1]["role"] == "assistant" and second[-1]["content"] == first.content
    assert fake.calls[0]["tools"][0]["type"] == "web_search_20260318"
    assert fake.calls[0]["output_config"] == {"effort": "low"}
    assert fake.calls[0]["_beta"] and fake.calls[0]["fallbacks"] == "default"
    assert "user_location" not in fake.calls[0]["tools"][0]


def test_pause_turn_limit(tmp_path):
    fake = FakeAnthropic([response("x", stop="pause_turn") for _ in range(10)])
    res = _ask(make_ctx(tmp_path, llm=fake))
    assert len(fake.calls) == llm.MAX_CONTINUATIONS + 1 and res.stop_reason == "pause_turn"


def test_search_version_fallback_chain(tmp_path):
    fake = FakeAnthropic([bad_request("tools.0: unknown type"), bad_request("tools.0: nope"), response("ok")])
    res = _ask(make_ctx(tmp_path, llm=fake))
    assert res.text == "ok"
    assert fake.calls[0]["tools"][0]["type"] == "web_search_20260318"
    assert fake.calls[1]["tools"][0]["type"] == "web_search_20250305"
    assert "tools" not in fake.calls[2]
    assert res.search_tool is None and res.notes


def test_fallback_param_rejected_retries_without(tmp_path):
    fake = FakeAnthropic([bad_request("fallbacks: unexpected beta"), response("ok")])
    res = _ask(make_ctx(tmp_path, llm=fake))
    assert res.text == "ok" and fake.calls[1]["_beta"] is False
    assert fake.calls[1]["tools"][0]["type"] == "web_search_20260318"


def test_haiku_no_fallback_param(tmp_path):
    fake = FakeAnthropic([response("ok")])
    _ask(make_ctx(tmp_path, llm=fake), model="claude-haiku-5-5", search_uses=0)
    assert fake.calls[0]["_beta"] is False and "tools" not in fake.calls[0]


def test_refusal_raises(tmp_path):
    fake = FakeAnthropic([response("", stop="refusal")])
    try:
        _ask(make_ctx(tmp_path, llm=fake))
    except llm.LLMError as e:
        assert "отказалась" in str(e)
    else:
        raise AssertionError


def test_usage_recorded_and_budget(tmp_path):
    fake = FakeAnthropic([response("ok", inp=100_000, out=50_000, searches=4)])
    ctx = make_ctx(tmp_path, llm=fake)
    res = _ask(ctx)
    # 0.1M*2 + 0.05M*10 + 4*0.01 = 0.2+0.5+0.04
    assert round(res.usd, 4) == 0.74
    rows = [json.loads(l) for l in (ctx.cfg.data_dir / "usage.jsonl").read_text().splitlines()]
    assert rows[0]["web_search_requests"] == 4
    assert round(month_usage(ctx.cfg.data_dir / "usage.jsonl", NOW).usd, 2) == 0.74
    ctx.cfg.raw["budget"]["monthly_usd"] = 0.9
    assert budget_mode(ctx.cfg, NOW) == "no_search"
    ctx.cfg.raw["budget"]["monthly_usd"] = 0.5
    assert budget_mode(ctx.cfg, NOW) == "stopped"
    try:
        _ask(ctx)
    except llm.LLMError as e:
        assert "бюджет" in str(e)


def test_no_search_mode_drops_tools(tmp_path):
    fake = FakeAnthropic([response("ok", inp=100_000), response("ok")])
    ctx = make_ctx(tmp_path, llm=fake)
    _ask(ctx)
    ctx.cfg.raw["budget"]["monthly_usd"] = 0.21
    _ask(ctx)
    assert "tools" not in fake.calls[1]


def test_extract_digest():
    assert llm.extract_digest("думаю <digest>черновик</digest> ещё <digest> финал </digest> хвост") == "финал"
    assert llm.extract_digest("пролог <digest>обрезано по max_tokens") == "обрезано по max_tokens"
    assert llm.extract_digest("без тегов") is None
    assert llm.extract_digest("<digest></digest>") is None
