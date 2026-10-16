from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from newsbot.config import Secrets, build_config
from newsbot.context import Ctx, make_http
from newsbot.state import State

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOW = datetime(2026, 10, 16, 5, 0, tzinfo=timezone.utc)  # 08:00 МСК, пятница


def base_raw() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def make_ctx(tmp_path, handler=None, raw=None, llm=None, now=NOW, secrets=None, dry_run=False):
    raw = raw if raw is not None else base_raw()
    cfg = build_config(raw, secrets or Secrets(telegram_bot_token="123:TEST", telegram_chat_id="42"),
                       tmp_path / "data")
    transport = httpx.MockTransport(handler) if handler else httpx.MockTransport(
        lambda req: httpx.Response(404))
    state = State.load(cfg.data_dir / "state.json", readonly=dry_run)
    return Ctx(cfg=cfg, state=state, http=make_http(5, transport), llm=llm,
               now=lambda: now, dry_run=dry_run)


@pytest.fixture
def fixtures():
    return FIXTURES
