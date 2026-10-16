"""Вызовы модели: Claude (веб-поиск с откатом версий, pause_turn, отказы) или Gemini; учёт расходов."""
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


def is_gemini(model: str) -> bool:
    return (model or "").startswith("gemini")


def supports_search(model: str) -> bool:
    """Веб-поиск есть только у Claude: у Gemini на бесплатном тарифе поиска нет."""
    return not is_gemini(model)


def _block_type(b) -> str:
    return b.get("type") if isinstance(b, dict) else getattr(b, "type", "")


def _block_text(b) -> str:
    return (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""


def ask(ctx, *, purpose: str, model: str, system: str, messages: list[dict], max_tokens: int,
        effort: str | None = None, search_uses: int = 0, output_format: dict | None = None) -> LLMResult:
    """Один «ход» модели с продолжениями pause_turn. Тексты всех итераций склеиваются."""
    if is_gemini(model):
        return _ask_gemini(ctx, purpose, model, system, messages, max_tokens, effort, output_format)
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


GEMINI_STOP = {"STOP": "end_turn", "MAX_TOKENS": "max_tokens"}


def _gemini_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(_block_text(b) for b in content if _block_type(b) == "text")


def _ask_gemini(ctx, purpose, model, system, messages, max_tokens, effort, output_format) -> LLMResult:
    from .gemini import GeminiError

    if getattr(ctx, "gemini", None) is None:
        raise LLMError("GEMINI_API_KEY не задан")
    if usage_mod.budget_mode(ctx.cfg, ctx.now()) == "stopped":
        raise LLMError("месячный бюджет на нейросеть исчерпан")
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": _gemini_text(m["content"])}]} for m in messages]
    gen: dict = {"maxOutputTokens": max_tokens}
    if effort:
        gen["thinkingConfig"] = {"thinkingLevel": effort}
    if output_format and output_format.get("schema"):
        gen["responseMimeType"] = "application/json"
        gen["responseJsonSchema"] = output_format["schema"]
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "generationConfig": gen}
    try:
        try:
            resp = ctx.gemini.generate(model, body)
        except GeminiError as e:
            if e.status_code != 400 or "thinkingConfig" not in gen:
                raise
            log.warning("Gemini 400 с thinkingConfig, повторяю без него: %s", e.message[:200])
            del gen["thinkingConfig"]
            resp = ctx.gemini.generate(model, body)
    except GeminiError as e:
        if e.status_code == 429:
            raise LLMError("лимит бесплатного тарифа Gemini исчерпан (429), попробуйте позже") from e
        if e.status_code == 400:
            raise LLMError(f"Gemini API отклонил запрос: {e.message}") from e
        raise LLMError(f"Gemini API: {e}") from e

    meta = resp.get("usageMetadata") or {}
    used = {"input_tokens": int(meta.get("promptTokenCount", 0) or 0),
            "output_tokens": int(meta.get("candidatesTokenCount", 0) or 0) + int(meta.get("thoughtsTokenCount", 0) or 0)}
    usd = usage_mod.record(ctx.cfg.data_dir / "usage.jsonl", ctx.now(), purpose,
                           resp.get("modelVersion") or model, used, ctx.cfg.section("pricing"))
    block = (resp.get("promptFeedback") or {}).get("blockReason")
    cands = resp.get("candidates") or []
    if block or not cands:
        raise LLMError(f"модель отказалась отвечать на запрос ({block or 'пустой ответ'})")
    cand = cands[0]
    finish = cand.get("finishReason") or "STOP"
    if finish not in GEMINI_STOP:
        raise LLMError(f"модель отказалась отвечать на запрос ({finish})")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return LLMResult(text=text, stop_reason=GEMINI_STOP[finish], usd=usd)


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
