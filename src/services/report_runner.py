from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session, joinedload

from src.analytics import format_report
from src.config import Settings
from src.db.models import AppSetting, Client
from src.services.goals_sync import selected_goal_ids
from src.services.message_delivery import (
    ReportDeliveryError,
    deliver_error_message,
    deliver_report_message,
    effective_report_channel,
    resolve_max_chat_id,
    resolve_telegram_chat_id,
)
from src.yandex_direct import YandexDirectClient, YandexDirectError, yesterday_and_day_before

logger = logging.getLogger(__name__)


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


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


def _client_delivery_configured(db: Session, settings: Settings, client: Client) -> bool:
    channel = effective_report_channel(db, settings)
    if channel in ("telegram", "both") and settings.telegram_bot_token:
        if resolve_telegram_chat_id(db, settings, client):
            return True
    if channel in ("max", "both") and settings.max_bot_token:
        if resolve_max_chat_id(db, settings, client):
            return True
    return False


def run_all_reports(db: Session, settings: Settings) -> dict[str, str | None]:
    clients = (
        db.query(Client)
        .options(joinedload(Client.goals))
        .filter(Client.is_active.is_(True))
        .order_by(Client.name)
        .all()
    )
    results: dict[str, str | None] = {}

    for client in clients:
        if not _client_delivery_configured(db, settings, client):
            results[client.name] = "Не настроена отправка (chat_id / токен мессенджера)"
            continue

        try:
            message = run_client_report(db, settings, client)
            deliver_report_message(db, settings, message, client=client)
            results[client.name] = None
            logger.info("Отчёт отправлен: %s", client.name)
        except ReportDeliveryError as exc:
            error_text = str(exc)
            results[client.name] = error_text
            logger.exception("Ошибка отправки для %s", client.name)
            deliver_error_message(db, settings, f"Клиент «{client.name}»: {error_text}", client=client)
        except Exception as exc:
            error_text = str(exc)
            results[client.name] = error_text
            logger.exception("Ошибка отчёта для %s", client.name)
            deliver_error_message(db, settings, f"Клиент «{client.name}»: {error_text}", client=client)

    set_setting(db, "last_report_run", datetime.utcnow().isoformat())
    return results
