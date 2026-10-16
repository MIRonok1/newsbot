import json

import httpx

from newsbot.context import make_http
from newsbot.sanitize import sanitize_html, split_message, to_plain
from newsbot.telegram import Telegram, TelegramError


def test_whitelist_and_escape():
    s = sanitize_html('<b>Ставка</b> <script>x</script> 5 < 6 & <h1>Заг</h1> <strong>ok</strong>')
    assert s == '<b>Ставка</b> &lt;script&gt;x&lt;/script&gt; 5 &lt; 6 &amp; &lt;h1&gt;Заг&lt;/h1&gt; <b>ok</b>'


def test_entities_preserved():
    assert sanitize_html("a &amp; b &lt; c &nbsp;") == "a &amp; b &lt; c &amp;nbsp;"


def test_balancing():
    assert sanitize_html("<b>жирный <i>курсив</b> хвост") == "<b>жирный <i>курсив</i></b> хвост"
    assert sanitize_html("текст</i> и <u>не закрыт") == "текст и <u>не закрыт</u>"


def test_links():
    assert sanitize_html('<a href="https://x.ru/a?b=1&c=2">ok</a>') == '<a href="https://x.ru/a?b=1&amp;c=2">ok</a>'
    assert sanitize_html('<a href="javascript:alert(1)">bad</a>') == "bad"
    assert sanitize_html('<a href="n12">ref</a> <b>x</b>') == "ref <b>x</b>"
    assert sanitize_html("<a href='http://a.b'>q</a>") == '<a href="http://a.b">q</a>'
    assert sanitize_html('<a onclick="x" href="https://a.b" >q</a>') == '<a href="https://a.b">q</a>'


def test_attrs_stripped_and_br():
    assert sanitize_html('<b class="x">a</b><br/>b<blockquote expandable>q</blockquote>') == \
        "<b>a</b>\nb<blockquote>q</blockquote>"


def test_split_long():
    para = "• <b>Пункт</b> " + "слово " * 120
    text = "\n\n".join([para] * 12)
    parts = split_message(text)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)
    assert all(p.count("<b>") == p.count("</b>") for p in parts)
    huge = "<b>" + "x" * 9000 + "</b>"
    parts = split_message(huge)
    assert all(len(p) <= 4096 for p in parts) and parts[0].startswith("<b>") and parts[0].endswith("</b>")


def test_to_plain():
    assert to_plain('<b>A</b> &amp; <a href="https://x.ru">Источник</a>') == "A & Источник (https://x.ru)"


def _tg(handler, sleeps=None):
    http = make_http(5, httpx.MockTransport(handler))
    return Telegram(http, "123:SECRET", "42", sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_send_parse_error_resends_plain():
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if body.get("parse_mode"):
            return httpx.Response(400, json={"ok": False, "error_code": 400,
                                              "description": "Bad Request: can't parse entities: unclosed tag"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    assert _tg(handler).send("<b>hi</b>") == 1
    assert len(sent) == 2 and "parse_mode" not in sent[1] and sent[1]["text"] == "hi"


def test_429_retry_after():
    sleeps, calls = [], []

    def handler(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many",
                                              "parameters": {"retry_after": 3}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    _tg(handler, sleeps).send("x")
    assert sleeps == [3.5] and len(calls) == 2


def test_long_message_split_and_no_token_in_error():
    texts = []

    def handler(req):
        texts.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {}})

    n = _tg(handler).send("\n\n".join(["абзац " * 100] * 20))
    assert n == len(texts) > 1

    def broken(req):
        raise httpx.ConnectError("failed for https://api.telegram.org/bot123:SECRET/sendMessage")

    try:
        _tg(broken).send("x")
    except TelegramError as e:
        assert "SECRET" not in str(e)
    else:
        raise AssertionError
