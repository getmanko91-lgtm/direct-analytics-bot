from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from src.db.models import ClientGoal
from src.yandex_direct import GoalInfo, MetrikaApiError, YandexDirectClient, YandexDirectError


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
    discovered: list[GoalInfo] = []
    warnings: list[str] = []

    if client.metrika_counter_id:
        try:
            discovered.extend(api.fetch_goals_from_metrika(client.metrika_counter_id))
        except MetrikaApiError as exc:
            warnings.append(str(exc))

    try:
        discovered.extend(api.fetch_goals_from_campaigns())
    except YandexDirectError as exc:
        if not discovered:
            raise
        warnings.append(f"Директ: {exc}")

    if not discovered:
        if warnings:
            raise YandexDirectError(" ".join(warnings))
        raise YandexDirectError(
            "Цели не найдены. Проверьте логин кабинета Директа и наличие активных кампаний."
        )

    existing = {g.goal_id: g for g in client.goals}
    updated: list[ClientGoal] = []

    for goal in discovered:
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
