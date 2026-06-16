from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.db.models import ClientGoal
from src.yandex_direct import (
    GoalInfo,
    LIVE_API_AUTH_HINT,
    LiveApiAuthError,
    MetrikaApiError,
    YandexDirectClient,
    YandexDirectError,
)


@dataclass
class GoalsSyncResult:
    goals: list[ClientGoal]
    warnings: list[str] = field(default_factory=list)


def sync_client_goals(
    db: Session,
    client,
    direct_token: str,
    metrika_token: str | None = None,
) -> GoalsSyncResult:
    api = YandexDirectClient(direct_token, client.yandex_login, metrika_token=metrika_token)
    discovered: dict[int, GoalInfo] = {}
    warnings: list[str] = []

    counter_ids: list[int] = []
    if client.metrika_counter_id:
        counter_ids.append(int(client.metrika_counter_id))

    try:
        for counter_id in api.fetch_counter_ids_from_campaigns():
            if counter_id not in counter_ids:
                counter_ids.append(counter_id)
    except YandexDirectError as exc:
        warnings.append(f"Не удалось прочитать счётчики из кампаний: {exc}")

    for counter_id in counter_ids:
        try:
            for goal in api.fetch_goals_from_metrika(counter_id):
                discovered[goal.goal_id] = goal
        except MetrikaApiError as exc:
            warnings.append(f"Метрика (счётчик {counter_id}): {exc}")

    try:
        for goal in api.fetch_goals_from_campaign_settings():
            if goal.goal_id not in discovered:
                discovered[goal.goal_id] = goal
    except YandexDirectError as exc:
        warnings.append(f"Настройки кампаний: {exc}")

    try:
        for goal in api.fetch_goals_from_campaigns():
            discovered[goal.goal_id] = goal
    except LiveApiAuthError:
        if not discovered:
            warnings.append(LIVE_API_AUTH_HINT)
        else:
            warnings.append(
                "Live API Директа (код 53) недоступен — список целей загружен через Метрику/API v5."
            )
    except YandexDirectError as exc:
        if not discovered:
            raise
        warnings.append(f"Директ: {exc}")

    if not discovered:
        if warnings:
            raise YandexDirectError(" ".join(warnings))
        raise YandexDirectError(
            "Цели не найдены. Укажите ID счётчика Метрики в карточке клиента и нажмите «Синхронизировать» снова."
        )

    existing = {g.goal_id: g for g in client.goals}
    updated: list[ClientGoal] = []

    for goal in discovered.values():
        if goal.goal_id in existing:
            row = existing[goal.goal_id]
            row.goal_name = goal.name
            updated.append(row)
        else:
            row = ClientGoal(
                client_id=client.id,
                goal_id=goal.goal_id,
                goal_name=goal.name,
                is_selected=False,
            )
            db.add(row)
            updated.append(row)

    db.commit()
    for row in updated:
        db.refresh(row)

    return GoalsSyncResult(
        goals=sorted(updated, key=lambda g: g.goal_name.lower()),
        warnings=warnings,
    )


def selected_goal_ids(client) -> list[int]:
    return [g.goal_id for g in client.goals if g.is_selected]
