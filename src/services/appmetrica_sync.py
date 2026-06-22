from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.appmetrica import (
    BUILTIN_GOALS,
    AppMetricaClient,
    AppMetricaError,
)
from src.config import Settings
from src.db.models import ClientAppMetricaGoal
from src.services.app_settings import get_setting


@dataclass
class AppMetricaSyncResult:
    goals: list[ClientAppMetricaGoal]
    warnings: list[str] = field(default_factory=list)


def resolve_appmetrica_token(db: Session, settings: Settings) -> str | None:
    token = (
        get_setting(db, "appmetrica_token")
        or settings.yandex_appmetrica_token
        or settings.yandex_metrika_token
        or settings.yandex_token
        or ""
    )
    token = token.strip()
    return token or None


def sync_client_appmetrica_goals(db: Session, client, settings: Settings) -> AppMetricaSyncResult:
    app_id = client.appmetrica_application_id
    if not app_id:
        raise AppMetricaError("Укажите ID приложения AppMetrica в карточке клиента.")

    token = resolve_appmetrica_token(db, settings)
    if not token:
        raise AppMetricaError(
            "Не задан токен AppMetrica. Укажите его в Настройках сервиса "
            "или в .env (YANDEX_APPMETRICA_TOKEN)."
        )

    api = AppMetricaClient(token)
    custom_events = api.fetch_events(int(app_id))

    discovered: dict[str, str] = {key: label for key, label in BUILTIN_GOALS}
    for event_name in custom_events:
        discovered[event_name] = event_name

    existing = {goal.event_key: goal for goal in client.appmetrica_goals}
    updated: list[ClientAppMetricaGoal] = []
    for event_key, event_label in discovered.items():
        if event_key in existing:
            row = existing[event_key]
            row.event_label = event_label
            updated.append(row)
        else:
            row = ClientAppMetricaGoal(
                client_id=client.id,
                event_key=event_key,
                event_label=event_label,
                role="",
            )
            db.add(row)
            updated.append(row)

    stale_keys = set(existing) - set(discovered)
    for event_key in stale_keys:
        row = existing[event_key]
        if row.role:
            row.role = ""
        updated = [goal for goal in updated if goal.event_key != event_key]
        db.delete(row)

    db.commit()
    for row in updated:
        db.refresh(row)

    return AppMetricaSyncResult(goals=sorted(updated, key=lambda g: g.event_label.lower()))


def selected_appmetrica_install_key(client) -> str | None:
    for goal in client.appmetrica_goals:
        if goal.role == "install":
            return goal.event_key
    return None


def selected_appmetrica_purchase_key(client) -> str | None:
    for goal in client.appmetrica_goals:
        if goal.role == "purchase":
            return goal.event_key
    return None
