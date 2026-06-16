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
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sync_client_goals(
    db: Session,
    client,
    direct_token: str,
    metrika_token: str | None = None,
) -> GoalsSyncResult:
    api = YandexDirectClient(direct_token, client.yandex_login, metrika_token=metrika_token)
    discovered: dict[int, GoalInfo] = {}
    sources: list[str] = []
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

    metrika_failed: list[tuple[int, str]] = []
    for counter_id in counter_ids:
        before = len(discovered)
        try:
            for goal in api.fetch_goals_from_metrika(counter_id):
                discovered[goal.goal_id] = goal
        except MetrikaApiError as exc:
            metrika_failed.append((counter_id, str(exc)))
        if len(discovered) > before and "Метрика" not in sources:
            sources.append("Метрика")

    if metrika_failed:
        unique_errors = {msg for _, msg in metrika_failed}
        if len(unique_errors) == 1:
            counters = ", ".join(str(cid) for cid, _ in metrika_failed)
            warnings.append(f"Метрика (счётчики {counters}): {metrika_failed[0][1]}")
        else:
            for counter_id, msg in metrika_failed:
                warnings.append(f"Метрика (счётчик {counter_id}): {msg}")

    before_v5 = len(discovered)
    try:
        for goal in api.fetch_goals_from_campaign_settings():
            if goal.goal_id not in discovered:
                discovered[goal.goal_id] = goal
    except YandexDirectError as exc:
        warnings.append(f"Настройки кампаний: {exc}")
    if len(discovered) > before_v5 and "настройки кампаний Директа" not in sources:
        sources.append("настройки кампаний Директа")

    before_live = len(discovered)
    try:
        for goal in api.fetch_goals_from_campaigns():
            discovered[goal.goal_id] = goal
    except LiveApiAuthError:
        if not discovered:
            warnings.append(LIVE_API_AUTH_HINT)
        else:
            warnings.append("Live API Директа (код 53) недоступен для этого токена.")
    except YandexDirectError as exc:
        if not discovered:
            raise
        warnings.append(f"Директ: {exc}")
    if len(discovered) > before_live and "Live API Директа" not in sources:
        sources.append("Live API Директа")

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

    if metrika_failed and "настройки кампаний Директа" in sources:
        warnings.append(
            "Показан неполный список целей (только из стратегий кампаний). "
            "Для всех целей с названиями добавьте YANDEX_METRIKA_TOKEN в .env на сервере."
        )

    return GoalsSyncResult(
        goals=sorted(updated, key=lambda g: g.goal_name.lower()),
        sources=sources,
        warnings=warnings,
    )


def selected_goal_ids(client) -> list[int]:
    return [g.goal_id for g in client.goals if g.is_selected]
