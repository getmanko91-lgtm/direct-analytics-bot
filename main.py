from __future__ import annotations



import argparse

import logging



import uvicorn

from apscheduler.schedulers.blocking import BlockingScheduler

from apscheduler.triggers.cron import CronTrigger

from zoneinfo import ZoneInfo



from src.config import Settings, load_settings

from src.db.database import SessionLocal, init_db

from src.db.seed import ensure_admin_user

from src.services.report_runner import run_all_reports

from src.web.app import create_app



logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",

)

logger = logging.getLogger("direct-analytics-bot")





def _parse_report_time(value: str) -> tuple[int, int]:

    parts = value.strip().split(":")

    if len(parts) != 2:

        raise ValueError("REPORT_TIME must be in HH:MM format")

    hour, minute = int(parts[0]), int(parts[1])

    if not (0 <= hour <= 23 and 0 <= minute <= 59):

        raise ValueError("REPORT_TIME must be a valid time")

    return hour, minute





def run_cli_reports(settings: Settings) -> None:

    init_db()

    db = SessionLocal()

    try:

        ensure_admin_user(db, settings)

        results = run_all_reports(db, settings)

        for name, error in results.items():

            if error:

                logger.error("%s: %s", name, error)

            else:

                logger.info("OK: %s", name)

    finally:

        db.close()





def start_cli_scheduler(settings: Settings) -> None:

    hour, minute = _parse_report_time(settings.report_time)

    tz = ZoneInfo(settings.timezone)



    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(

        run_cli_reports,

        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),

        kwargs={"settings": settings},

        id="daily_yandex_direct_report",

        replace_existing=True,

        misfire_grace_time=3600,

    )



    logger.info(

        "Планировщик запущен. Отчёт будет отправляться ежедневно в %02d:%02d (%s)",

        hour,

        minute,

        settings.timezone,

    )

    scheduler.start()





def start_web(settings: Settings) -> None:

    app = create_app(settings)

    uvicorn.run(app, host=settings.web_host, port=settings.web_port, log_level="info")





def main(argv: list[str] | None = None) -> int:

    parser = argparse.ArgumentParser(description="Direct Analytics Bot")

    parser.add_argument(

        "mode",

        nargs="?",

        choices=("web", "once", "schedule"),

        default="web",

        help="web — веб-интерфейс (по умолчанию); once — CLI-отправка; schedule — CLI-планировщик",

    )

    args = parser.parse_args(argv)



    try:

        settings = load_settings()

    except ValueError as exc:

        logger.error("%s", exc)

        return 1



    if args.mode == "web":

        start_web(settings)

        return 0



    if args.mode == "once":

        try:

            run_cli_reports(settings)

        except Exception:

            return 1

        return 0



    try:

        start_cli_scheduler(settings)

    except (KeyboardInterrupt, SystemExit):

        logger.info("Планировщик остановлен")

        return 0

    except Exception:

        logger.exception("Ошибка планировщика")

        return 1





if __name__ == "__main__":

    raise SystemExit(main())

