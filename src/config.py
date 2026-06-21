from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

VAT_RATE = float(os.getenv("VAT_RATE", "0.22"))
REPORT_CHANNELS = frozenset({"telegram", "max", "both"})


@dataclass(frozen=True)
class Settings:
    yandex_token: str
    yandex_metrika_token: str | None
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_proxy: str | None
    max_bot_token: str
    max_chat_id: str
    report_channel: str
    report_time: str
    timezone: str
    secret_key: str
    admin_username: str
    admin_password: str
    web_host: str
    web_port: int
    vat_rate: float


def load_settings() -> Settings:
    telegram_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    max_token = (os.getenv("MAX_BOT_TOKEN") or "").strip()
    report_channel = (os.getenv("REPORT_CHANNEL") or "telegram").strip().lower()
    if report_channel not in REPORT_CHANNELS:
        report_channel = "telegram"

    missing = [
        name
        for name, value in (
            ("YANDEX_DIRECT_TOKEN", os.getenv("YANDEX_DIRECT_TOKEN")),
            ("SECRET_KEY", os.getenv("SECRET_KEY")),
        )
        if not value
    ]
    if not telegram_token and not max_token:
        missing.append("TELEGRAM_BOT_TOKEN or MAX_BOT_TOKEN")
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Settings(
        yandex_token=os.environ["YANDEX_DIRECT_TOKEN"],
        yandex_metrika_token=os.getenv("YANDEX_METRIKA_TOKEN") or None,
        telegram_bot_token=telegram_token,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_proxy=(os.getenv("TELEGRAM_PROXY") or os.getenv("HTTPS_PROXY") or "").strip() or None,
        max_bot_token=max_token,
        max_chat_id=os.getenv("MAX_CHAT_ID", ""),
        report_channel=report_channel,
        report_time=os.getenv("REPORT_TIME", "09:00"),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow"),
        secret_key=os.environ["SECRET_KEY"],
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "changeme"),
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        web_port=int(os.getenv("WEB_PORT", "8080")),
        vat_rate=VAT_RATE,
    )
