from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, joinedload

from src.analytics import format_report
from src.config import Settings
from src.db.models import Client
from src.services.analytics_table import (
    fetch_analytics_table_cached,
    find_conversion_drought_clients,
    format_analytics_telegram,
)
from src.services.app_settings import get_setting, set_setting
from src.services.goals_sync import selected_goal_ids
from src.services.message_delivery import (
    ReportDeliveryError,
    deliver_error_message,
    deliver_report_message,
    effective_report_channel,
    resolve_max_bot_token,
    resolve_max_chat_id,
    resolve_telegram_chat_id,
)
from src.yandex_direct import YandexDirectClient, YandexDirectError, yesterday_and_day_before

logger = logging.getLogger(__name__)


def run_client_report(
    db: Session,
    settings: Settings,
    client: Client,
) -> str:
    goal_ids = selected_goal_ids(client)
    if not goal_ids:
        raise ValueError(f"У клиента «{client.name}» не выбраны цели для конверсий")

    api = YandexDirectClient(settings.yandex_token, client.yandex_login)
    yesterday_date, day_before_date = yesterday_and_day_before()

    stats_by_date = api.fetch_period_stats(
        day_before_date,
        yesterday_date,
        goal_ids=goal_ids,
        attribution_model=client.attribution_model,
        vat_rate=settings.vat_rate,
    )
    yesterday_stats = stats_by_date.get(yesterday_date)
    day_before_stats = stats_by_date.get(day_before_date)

    if yesterday_stats is None:
        raise YandexDirectError(f"Нет данных за {yesterday_date} для клиента {client.name}")

    selected_names = [g.goal_name for g in client.goals if g.is_selected]
    return format_report(
        yesterday=yesterday_stats,
        day_before=day_before_stats,
        spend_alert_threshold=client.spend_alert_threshold,
        client_name=client.name,
        goal_names=selected_names,
        vat_percent=int(settings.vat_rate * 100),
    )


def _summary_delivery_configured(db: Session, settings: Settings) -> bool:
    channel = effective_report_channel(db, settings)
    if channel in ("telegram", "both") and settings.telegram_bot_token:
        if resolve_telegram_chat_id(db, settings):
            return True
    if channel in ("max", "both") and resolve_max_bot_token(db, settings):
        if resolve_max_chat_id(db, settings):
            return True
    return False


def run_daily_summary_report(db: Session, settings: Settings) -> None:
    if not _summary_delivery_configured(db, settings):
        raise ReportDeliveryError("Не настроена отправка сводки (chat_id / токен мессенджера)")

    yesterday = date.today() - timedelta(days=1)
    rows = fetch_analytics_table_cached(db, settings, yesterday, yesterday)
    drought = find_conversion_drought_clients(db, settings, yesterday)
    message = format_analytics_telegram(
        rows,
        yesterday,
        yesterday,
        conversion_drought_clients=drought,
    )
    deliver_report_message(db, settings, message)
    set_setting(db, "last_report_run", datetime.utcnow().isoformat())
    logger.info("Ежедневная сводка за %s отправлена", yesterday.isoformat())


def run_all_reports(db: Session, settings: Settings) -> dict[str, str | None]:
    try:
        run_daily_summary_report(db, settings)
        return {"Сводка": None}
    except ReportDeliveryError as exc:
        error_text = str(exc)
        logger.exception("Ошибка отправки сводки")
        deliver_error_message(db, settings, error_text)
        return {"Сводка": error_text}
    except Exception as exc:
        error_text = str(exc)
        logger.exception("Ошибка формирования сводки")
        deliver_error_message(db, settings, error_text)
        return {"Сводка": error_text}


def effective_report_schedule(db: Session, settings: Settings) -> tuple[str, str]:
    report_time = (get_setting(db, "report_time") or settings.report_time or "09:00").strip()
    timezone = (get_setting(db, "timezone") or settings.timezone or "Europe/Moscow").strip()
    return report_time, timezone
