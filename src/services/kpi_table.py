from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.direct_report_rows import CACHE_TTL_SECONDS, fetch_campaign_performance_rows_cached
from src.services.parallel_fetch import map_parallel
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import _parse_float, conversions_for_goal

KPI_CAMPAIGN_PREFIXES = ("КОНВЕР", "МК", "ЕПК", "ТМК")


def _is_kpi_campaign(campaign_name: str) -> bool:
    upper = campaign_name.strip().upper()
    return any(upper.startswith(prefix) for prefix in KPI_CAMPAIGN_PREFIXES)


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
    return map_parallel(
        lambda client: _kpi_row_for_client(client, settings, date_from, date_to),
        clients,
    )


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
        ttl_seconds=CACHE_TTL_SECONDS,
    )


def _kpi_row_for_client(
    client: Client,
    settings: Settings,
    date_from: date,
    date_to: date,
) -> KpiRow:
    selected_goals = [g for g in client.goals if g.is_selected]
    if not selected_goals:
        return KpiRow(
            client_id=client.id,
            client_name=client.name,
            directologist=client.directologist or "—",
            spend=0.0,
            conversions=0.0,
            cpa=None,
            error="Не выбраны цели",
        )

    goal_ids = [g.goal_id for g in selected_goals]
    try:
        rows = fetch_campaign_performance_rows_cached(settings, client, date_from, date_to)
        spend_raw, conversions = _aggregate_kpi_campaigns(rows, goal_ids, client.attribution_model)
        spend = cost_with_vat(spend_raw, settings.vat_rate)
        cpa = (spend / conversions) if conversions > 0 else None
        return KpiRow(
            client_id=client.id,
            client_name=client.name,
            directologist=client.directologist or "—",
            spend=spend,
            conversions=conversions,
            cpa=cpa,
        )
    except Exception as exc:
        return KpiRow(
            client_id=client.id,
            client_name=client.name,
            directologist=client.directologist or "—",
            spend=0.0,
            conversions=0.0,
            cpa=None,
            error=str(exc)[:300],
        )


def _aggregate_kpi_campaigns(
    rows: list[dict[str, str]],
    goal_ids: list[int],
    attribution_model: str,
) -> tuple[float, float]:
    spend_raw = 0.0
    conversions = 0.0
    for row in rows:
        campaign_name = (row.get("CampaignName") or "").strip()
        if not _is_kpi_campaign(campaign_name):
            continue
        spend_raw += _parse_float(row.get("Cost", "0"))
        for gid in goal_ids:
            conversions += conversions_for_goal(row, gid, attribution_model)
    return spend_raw, conversions
