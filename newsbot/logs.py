"""Логи в data/newsbot.log с ротацией. Токен бота в логи не попадает."""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


class RedactFilter(logging.Filter):
    """Подменяет секреты в тексте записи на ***."""

    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self.secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging(data_dir: Path, secrets: list[str] | None = None, level: int = logging.INFO,
                  to_file: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    redact = RedactFilter(secrets or [])

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    stream.addFilter(redact)
    root.addHandler(stream)

    if to_file:
        data_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            data_dir / "newsbot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        fh.addFilter(redact)
        root.addHandler(fh)

    # httpx пишет URL запросов, а в URL Bot API есть токен
    for name in ("httpx", "httpcore", "httpx2", "anthropic", "apscheduler.executors"):
        logging.getLogger(name).setLevel(logging.WARNING)
