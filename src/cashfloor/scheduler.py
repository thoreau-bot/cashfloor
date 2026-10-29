"""Daily sync scheduler (runs inside `cashfloor serve`)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import Config
from .db import Database
from .sync import run_sync

log = logging.getLogger("cashfloor.scheduler")


def start_scheduler(cfg: Config) -> BackgroundScheduler:
    hh, mm = cfg.scheduler.daily_sync_time.split(":")

    def job() -> None:
        db = Database(cfg.db_path)
        try:
            s = run_sync(cfg, db)
            log.info("daily sync: ok=%s failed=%s", s.providers_ok, s.providers_failed)
        finally:
            db.close()

    sched = BackgroundScheduler(timezone=cfg.general.timezone)
    sched.add_job(job, CronTrigger(hour=int(hh), minute=int(mm)), id="daily_sync", replace_existing=True)
    sched.start()
    return sched
