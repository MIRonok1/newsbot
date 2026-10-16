"""Вызовы Claude: веб-поиск с откатом версий, pause_turn, отказы, учёт расходов."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import usage as usage_mod

log = logging.getLogger(__name__)

MAX_CONTINUATIONS = 5
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    stop_reason: str
    usd: float = 0.0
    searches: int = 0
    search_tool: str | None = None
    notes: list[str] = field(default_factory=list)


def _bad_request_cls():
    try:
        import anthropic

        return anthropic.BadRequestError
    except ImportError:  # pragma: no cover
        return ()


def _supports_fallback(model: str) -> bool:
    return model.startswith(("claude-sonnet", "claude-opus", "claude-fable"))


def _block_type(b) -> str:
    return b.get("type") if isinstance(b, dict) else getattr(b, "type", "")


def _block_text(b) -> str:
    return (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""


def ask(ctx, *, purpose: str, model: str, system: str, messages: list[dict], max_tokens: int,
        effort: str | None = None, search_uses: int = 0, output_format: dict | None = None) -> LLMResult:
    """Один «ход» модели с продолжениями pause_turn. Тексты всех итераций склеиваются."""
    if ctx.llm is None:
        raise LLMError("ANTHROPIC_API_KEY не задан")
    cfg = ctx.cfg
    mode = usage_mod.budget_mode(cfg, ctx.now())
    if mode == "stopped":
        raise LLMError("месячный бюджет на Claude API исчерпан")
    notes = []
    versions: list[str | None] = [None]
    if search_uses and cfg.get("web_search.enabled", True):
        if mode == "no_search":
            notes.append("поиск отключён: израсходовано больше порога бюджета")
        else:
            chain = [cfg.get("web_search.tool_version"), cfg.get("web_search.fallback_version")]
            versions = [v for i, v in enumerate(chain) if v and v not in chain[:i]] + [None]
    use_fallback = bool(cfg.get("models.refusal_fallback", True)) and _supports_fallback(model)
    bad_request = _bad_request_cls()

    last_error: Exception | None = None
    for version in versions:
        tools = [{"type": version, "name": "web_search", "max_uses": search_uses}] if version else []
        for attempt_fallback in ([True, False] if use_fallback else [False]):
            try:
                result = _run(ctx, purpose, model, system, messages, max_tokens, effort, tools,
                              output_format, attempt_fallback)
                result.search_tool = version
                result.notes = notes + result.notes
                if version != versions[0]:
                    result.notes.append(f"веб-поиск: использована версия {version or 'без поиска'}")
                return result
            except bad_request as e:  # type: ignore[misc]
                last_error = e
                msg = str(e)
                log.warning("Claude 400 (поиск=%s, fallback=%s): %s", version, attempt_fallback, msg[:300])
                if attempt_fallback and (version is None or re.search(r"fallback|beta", msg, re.I)):
                    continue  # повторить без fallbacks
                break  # перейти к следующей версии поиска
    raise LLMError(f"Claude API отклонил запрос: {last_error}")


def _run(ctx, purpose, model, system, messages, max_tokens, effort, tools, output_format, fallback) -> LLMResult:
    msgs = [dict(m) for m in messages]
    texts: list[str] = []
    total_usd = 0.0
    searches = 0
    output_config = {}
    if effort:
        output_config["effort"] = effort
    if output_format:
        output_config["format"] = output_format
    stop = ""
    for _ in range(MAX_CONTINUATIONS + 1):
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=msgs)
        if tools:
            kwargs["tools"] = tools
        if output_config:
            kwargs["output_config"] = output_config
        if fallback:
            resp = ctx.llm.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        else:
            resp = ctx.llm.messages.create(**kwargs)
        usd = usage_mod.record(ctx.cfg.data_dir / "usage.jsonl", ctx.now(), purpose,
                               getattr(resp, "model", model) or model, resp.usage, ctx.cfg.section("pricing"))
        total_usd += usd
        su = getattr(resp.usage, "server_tool_use", None)
        searches += int(getattr(su, "web_search_requests", 0) or 0) if su else 0
        content = list(resp.content or [])
        texts.extend(_block_text(b) for b in content if _block_type(b) == "text")
        stop = resp.stop_reason or ""
        if stop == "refusal":
            raise LLMError("модель отказалась отвечать на запрос")
        if stop != "pause_turn":
            break
        # pause_turn: возвращаем ответ ассистента как есть и продолжаем
        if msgs and msgs[-1]["role"] == "assistant":
            msgs[-1] = {"role": "assistant", "content": list(msgs[-1]["content"]) + content}
        else:
            msgs.append({"role": "assistant", "content": content})
    else:
        log.warning("pause_turn повторялся больше %d раз — беру что есть", MAX_CONTINUATIONS)
    return LLMResult(text="".join(texts), stop_reason=stop, usd=total_usd, searches=searches)


def extract_digest(text: str) -> str | None:
    """Последний блок <digest>…</digest>; если закрывающего нет — хвост после <digest>."""
    blocks = re.findall(r"<digest>(.*?)</digest>", text, re.S | re.I)
    if blocks:
        return blocks[-1].strip() or None
    idx = text.lower().rfind("<digest>")
    if idx != -1:
        tail = text[idx + len("<digest>"):].strip()
        return tail or None
    return None
