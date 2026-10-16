"""Расписание: сводки 08:00/20:00 и проверки срочного, с догоном пропущенного."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .alerts import run_alerts
from .digest import run_digest

log = logging.getLogger(__name__)
GRACE = 3600  # пропущенная сводка догоняется в течение часа


def _hhmm(value: str) -> time:
    h, m = str(value).split(":")
    return time(int(h), int(m))


def scheduled_digest(ctx, kind: str) -> None:
    # Сводка ждёт, пока закончится проверка срочного (не дольше 10 минут)
    if not ctx.heavy_lock.acquire(timeout=600):
        log.error("сводка %s: не дождался завершения другой задачи", kind)
        return
    try:
        run_digest(ctx, kind, scheduled=True)
    except Exception:
        log.exception("плановая сводка %s упала", kind)
        if ctx.telegram:
            try:
                ctx.telegram.send(f"⚠️ Плановая сводка ({kind}) не получилась — подробности в логе.")
            except Exception:
                pass
    finally:
        ctx.heavy_lock.release()


def scheduled_alerts(ctx) -> None:
    if not ctx.heavy_lock.acquire(blocking=False):
        log.info("алерты: занято сводкой, пропускаю проверку")
        return
    try:
        run_alerts(ctx)
    except Exception:
        log.exception("проверка срочного упала")
    finally:
        ctx.heavy_lock.release()


def missed_digest(ctx, now: datetime | None = None) -> str | None:
    """Какую сводку догнать после простоя: слот был < часа назад, а сводки после него не было."""
    tz = ctx.cfg.tz
    now = (now or ctx.now()).astimezone(tz)
    for kind in ("morning", "evening"):
        slot = datetime.combine(now.date(), _hhmm(ctx.cfg.get(f"schedule.{kind}")), tz)
        if slot <= now < slot + timedelta(seconds=GRACE):
            last = ctx.state["last_digest_at"].get(kind)
            if not last or datetime.fromisoformat(last) < slot:
                return kind
    return None


def build_scheduler(ctx) -> BackgroundScheduler:
    cfg = ctx.cfg
    sched = BackgroundScheduler(timezone=cfg.tz, job_defaults={
        "coalesce": True, "max_instances": 1, "misfire_grace_time": GRACE})
    for kind in ("morning", "evening"):
        t = _hhmm(cfg.get(f"schedule.{kind}"))
        sched.add_job(scheduled_digest, CronTrigger(hour=t.hour, minute=t.minute, timezone=cfg.tz),
                      args=[ctx, kind], id=f"digest_{kind}", name=f"Сводка ({kind})")
    sched.add_job(scheduled_alerts,
                  CronTrigger(hour=cfg.get("schedule.alerts_hours", "7-23"),
                              minute=str(cfg.get("schedule.alerts_minutes", "15,45")), timezone=cfg.tz),
                  args=[ctx], id="alerts", name="Срочное")
    return sched


def start(ctx) -> BackgroundScheduler:
    sched = build_scheduler(ctx)
    sched.start()
    kind = missed_digest(ctx)
    if kind:
        log.info("догоняю пропущенную сводку: %s", kind)
        sched.add_job(scheduled_digest, args=[ctx, kind], id=f"catchup_{kind}", name="Догон сводки")
    return sched
