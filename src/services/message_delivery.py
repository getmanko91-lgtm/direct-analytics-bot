from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from src.config import Settings
from src.db.models import Client
from src.max_notifier import MaxError, MaxNotifier
from src.services.app_settings import get_setting
from src.telegram_notifier import TelegramError, TelegramNotifier

logger = logging.getLogger(__name__)

REPORT_CHANNELS = ("telegram", "max", "both")


class ReportDeliveryError(RuntimeError):
    pass


def effective_report_channel(db: Session, settings: Settings) -> str:
    channel = (get_setting(db, "report_channel") or settings.report_channel or "telegram").strip().lower()
    if channel not in REPORT_CHANNELS:
        return "telegram"
    return channel


def resolve_telegram_chat_id(
    db: Session,
    settings: Settings,
    client: Client | None = None,
) -> str:
    if client and client.telegram_chat_id:
        return client.telegram_chat_id.strip()
    return (get_setting(db, "telegram_chat_id") or settings.telegram_chat_id or "").strip()


def resolve_max_chat_id(
    db: Session,
    settings: Settings,
    client: Client | None = None,
) -> str:
    if client and client.max_chat_id:
        return client.max_chat_id.strip()
    return (get_setting(db, "max_chat_id") or settings.max_chat_id or "").strip()


def resolve_max_bot_token(db: Session, settings: Settings) -> str:
    return (get_setting(db, "max_bot_token") or settings.max_bot_token or "").strip()


def deliver_report_message(
    db: Session,
    settings: Settings,
    message: str,
    *,
    client: Client | None = None,
) -> str:
    """Отправляет отчёт в выбранный мессенджер(ы). Возвращает текст для UI."""
    channel = effective_report_channel(db, settings)
    errors: list[str] = []
    sent: list[str] = []

    if channel in ("telegram", "both"):
        if not settings.telegram_bot_token:
            errors.append("Telegram: не задан TELEGRAM_BOT_TOKEN в .env")
        else:
            chat_id = resolve_telegram_chat_id(db, settings, client)
            if not chat_id:
                errors.append("Telegram: не указан chat_id")
            else:
                try:
                    TelegramNotifier(
                        settings.telegram_bot_token,
                        chat_id,
                        proxy=settings.telegram_proxy,
                    ).send_message(message)
                    sent.append("Telegram")
                except TelegramError as exc:
                    errors.append(f"Telegram: {exc}")

    if channel in ("max", "both"):
        max_token = resolve_max_bot_token(db, settings)
        if not max_token:
            errors.append("MAX: не задан токен бота (Настройки или MAX_BOT_TOKEN в .env)")
        else:
            chat_id = resolve_max_chat_id(db, settings, client)
            if not chat_id:
                errors.append("MAX: не указан chat_id")
            else:
                try:
                    MaxNotifier(max_token, chat_id).send_message(message)
                    sent.append("MAX")
                except MaxError as exc:
                    errors.append(f"MAX: {exc}")

    if not sent:
        raise ReportDeliveryError("; ".join(errors) if errors else "Не настроена отправка отчётов")

    if errors:
        logger.warning("Частичная отправка отчёта: sent=%s errors=%s", sent, errors)
        return f"Отправлено в {', '.join(sent)}. Ошибки: {'; '.join(errors)}"

    if len(sent) == 1:
        return sent[0]
    return f"{sent[0]} и {sent[1]}"


def deliver_error_message(
    db: Session,
    settings: Settings,
    error: str,
    *,
    client: Client | None = None,
) -> None:
    channel = effective_report_channel(db, settings)
    text = f"❌ <b>Ошибка direct-analytics-bot</b>\n\n{error}"

    if channel in ("telegram", "both") and settings.telegram_bot_token:
        chat_id = resolve_telegram_chat_id(db, settings, client)
        if chat_id:
            try:
                TelegramNotifier(
                    settings.telegram_bot_token,
                    chat_id,
                    proxy=settings.telegram_proxy,
                ).send_error(error)
            except Exception:
                logger.exception("Не удалось отправить ошибку в Telegram")

    if channel in ("max", "both") and resolve_max_bot_token(db, settings):
        chat_id = resolve_max_chat_id(db, settings, client)
        if chat_id:
            try:
                MaxNotifier(resolve_max_bot_token(db, settings), chat_id).send_error(error)
            except Exception:
                logger.exception("Не удалось отправить ошибку в MAX")
