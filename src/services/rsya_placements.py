from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import YandexDirectClient, conversions_for_goal, _parse_float, _parse_int


@dataclass(frozen=True)
class RsyaPlacementRow:
    placement: str
    spend: float
    impressions: int
    clicks: int
    conversions: float


@dataclass(frozen=True)
class RsyaPlacementsReport:
    client: Client
    placements: tuple[RsyaPlacementRow, ...]
    total_wasted_spend: float
    error: str | None = None


def fetch_rsya_zero_conversion_placements(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    min_clicks: int = 1,
    min_spend: float = 0.0,
) -> RsyaPlacementsReport | None:
    client = (
        db.query(Client)
        .options(joinedload(Client.goals))
        .filter(Client.id == client_id)
        .first()
    )
    if not client:
        return None

    selected_goals = [g for g in client.goals if g.is_selected]
    if not selected_goals:
        return RsyaPlacementsReport(
            client=client,
            placements=(),
            total_wasted_spend=0.0,
            error="Не выбраны цели — выберите цели для расчёта конверсий по площадкам.",
        )

    goal_ids = [g.goal_id for g in selected_goals]
    try:
        api = YandexDirectClient(settings.yandex_token, client.yandex_login)
        raw_rows = api.fetch_rsya_placement_report(
            date_from,
            date_to,
            goal_ids,
            client.attribution_model,
        )
        placements = _filter_zero_conversion_placements(
            raw_rows,
            goal_ids,
            client.attribution_model,
            settings.vat_rate,
            min_clicks=min_clicks,
            min_spend=min_spend,
        )
        wasted = sum(row.spend for row in placements)
        return RsyaPlacementsReport(
            client=client,
            placements=tuple(placements),
            total_wasted_spend=wasted,
        )
    except Exception as exc:
        return RsyaPlacementsReport(
            client=client,
            placements=(),
            total_wasted_spend=0.0,
            error=str(exc)[:400],
        )


def fetch_rsya_zero_conversion_placements_cached(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    min_clicks: int = 1,
    min_spend: float = 0.0,
) -> RsyaPlacementsReport | None:
    key = (
        "rsya_placements",
        settings.yandex_token,
        client_id,
        date_from.isoformat(),
        date_to.isoformat(),
        min_clicks,
        round(min_spend, 2),
    )
    return get_or_set(
        key,
        lambda: fetch_rsya_zero_conversion_placements(
            db,
            settings,
            client_id,
            date_from,
            date_to,
            min_clicks=min_clicks,
            min_spend=min_spend,
        ),
        ttl_seconds=120,
    )


def _filter_zero_conversion_placements(
    rows: list[dict[str, str]],
    goal_ids: list[int],
    attribution_model: str,
    vat_rate: float,
    *,
    min_clicks: int,
    min_spend: float,
) -> list[RsyaPlacementRow]:
    result: list[RsyaPlacementRow] = []
    min_spend_with_vat = cost_with_vat(min_spend, vat_rate) if min_spend > 0 else 0.0

    for row in rows:
        placement = row.get("Placement", "").strip()
        if not placement or placement == "—":
            continue

        cost_raw = _parse_float(row.get("Cost", "0"))
        spend = cost_with_vat(cost_raw, vat_rate)
        clicks = _parse_int(row.get("Clicks", "0"))
        impressions = _parse_int(row.get("Impressions", "0"))
        conversions = sum(
            conversions_for_goal(row, goal_id, attribution_model) for goal_id in goal_ids
        )

        if conversions > 0:
            continue
        if clicks < min_clicks:
            continue
        if min_spend_with_vat > 0 and spend < min_spend_with_vat:
            continue

        result.append(
            RsyaPlacementRow(
                placement=placement,
                spend=spend,
                impressions=impressions,
                clicks=clicks,
                conversions=0.0,
            )
        )

    result.sort(key=lambda item: item.spend, reverse=True)
    return result
