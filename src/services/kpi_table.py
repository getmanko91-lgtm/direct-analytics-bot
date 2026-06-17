from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import (
    MAX_GOALS_PER_REQUEST,
    YandexDirectClient,
    _chunked,
    _merge_conversion_columns,
    _parse_float,
    conversions_for_goal,
)

KONVER_PREFIX = "КОНВЕР"


@dataclass(frozen=True)
class KpiRow:
    client_id: int
    client_name: str
    directologist: str
    spend: float
    conversions: float
    cpa: float | None
    error: str | None = None


def fetch_kpi_table(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[KpiRow]:
    query = db.query(Client).options(joinedload(Client.goals)).order_by(Client.name)
    if active_only:
        query = query.filter(Client.is_active.is_(True))
    clients = query.all()

    result: list[KpiRow] = []
    for client in clients:
        selected_goals = [g for g in client.goals if g.is_selected]
        if not selected_goals:
            result.append(
                KpiRow(
                    client_id=client.id,
                    client_name=client.name,
                    directologist=client.directologist or "—",
                    spend=0.0,
                    conversions=0.0,
                    cpa=None,
                    error="Не выбраны цели",
                )
            )
            continue

        goal_ids = [g.goal_id for g in selected_goals]
        try:
            api = YandexDirectClient(settings.yandex_token, client.yandex_login)
            rows = _fetch_all_report_rows(api, date_from, date_to, goal_ids, client.attribution_model)
            spend_raw, conversions = _aggregate_kpi_for_konver(rows, goal_ids, client.attribution_model)
            spend = cost_with_vat(spend_raw, settings.vat_rate)
            cpa = (spend_raw / conversions) if conversions > 0 else None
            result.append(
                KpiRow(
                    client_id=client.id,
                    client_name=client.name,
                    directologist=client.directologist or "—",
                    spend=spend,
                    conversions=conversions,
                    cpa=cpa,
                )
            )
        except Exception as exc:
            result.append(
                KpiRow(
                    client_id=client.id,
                    client_name=client.name,
                    directologist=client.directologist or "—",
                    spend=0.0,
                    conversions=0.0,
                    cpa=None,
                    error=str(exc)[:300],
                )
            )
    return result


def fetch_kpi_table_cached(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[KpiRow]:
    key = ("kpi_table", settings.yandex_token, date_from.isoformat(), date_to.isoformat(), active_only)
    return get_or_set(
        key,
        lambda: fetch_kpi_table(db, settings, date_from, date_to, active_only),
        ttl_seconds=90,
    )


def _fetch_all_report_rows(api, date_from, date_to, goal_ids, attribution_model):
    if not goal_ids:
        return api._fetch_report(date_from, date_to, [], attribution_model)

    merged: dict[tuple[str, str], dict[str, str]] = {}
    for chunk in _chunked(goal_ids, MAX_GOALS_PER_REQUEST):
        chunk_rows = api._fetch_report(date_from, date_to, list(chunk), attribution_model)
        for row in chunk_rows:
            key = (row.get("Date", ""), row.get("CampaignId", "") or row.get("CampaignName", "—"))
            if key not in merged:
                merged[key] = dict(row)
            else:
                _merge_conversion_columns(merged[key], row, list(chunk))
    return list(merged.values())


def _aggregate_kpi_for_konver(
    rows: list[dict[str, str]],
    goal_ids: list[int],
    attribution_model: str,
) -> tuple[float, float]:
    spend_raw = 0.0
    conversions = 0.0
    for row in rows:
        campaign_name = (row.get("CampaignName") or "").strip()
        if not campaign_name.upper().startswith(KONVER_PREFIX):
            continue
        spend_raw += _parse_float(row.get("Cost", "0"))
        for gid in goal_ids:
            conversions += conversions_for_goal(row, gid, attribution_model)
    return spend_raw, conversions
