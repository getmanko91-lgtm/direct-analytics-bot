from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from zoneinfo import ZoneInfo

from src.config import Settings, load_settings
from src.db.database import SessionLocal, init_db
from src.db.seed import ensure_admin_user
from src.services.report_runner import run_all_reports
from src.web.routes import router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
scheduler = BackgroundScheduler()


def _parse_report_time(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _schedule_daily_job(settings: Settings) -> None:
    hour, minute = _parse_report_time(settings.report_time)
    tz = ZoneInfo(settings.timezone)

    scheduler.add_job(
        _run_scheduled_reports,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_reports",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Планировщик: ежедневно в %02d:%02d (%s)", hour, minute, settings.timezone)


def _run_scheduled_reports() -> None:
    settings = load_settings()
    db = SessionLocal()
    try:
        run_all_reports(db, settings)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    try:
        init_db()
        db = SessionLocal()
        try:
            ensure_admin_user(db, settings)
        finally:
            db.close()

        _schedule_daily_job(settings)
        scheduler.start()
        logger.info("Веб-сервис запущен на http://%s:%s", settings.web_host, settings.web_port)
    except Exception:
        logger.exception("Ошибка при запуске приложения")
        raise
    yield
    scheduler.shutdown(wait=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Direct Analytics Bot", lifespan=lifespan)
    app.state.settings = settings

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="dab_session",
        max_age=60 * 60 * 24 * 14,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(router)
    return app
