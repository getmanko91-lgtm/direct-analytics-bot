from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.client_campaigns import ClientCampaignReport, _aggregate_campaigns
from src.services.cpa_style import weekly_budget
from src.services.direct_report_rows import CACHE_TTL_SECONDS
from src.services.rsya_placements import RsyaPlacementsReport, _filter_zero_conversion_placements
from src.services.runtime_cache import get_or_set
from src.yandex_direct import YandexDirectClient


@dataclass(frozen=True)
class ClientAnalyticsBundle:
    report: ClientCampaignReport
    placements: RsyaPlacementsReport


def fetch_client_analytics_bundle(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    min_clicks: int = 1,
    min_spend: float = 0.0,
) -> ClientAnalyticsBundle | None:
    client = (
        db.query(Client)
        .options(joinedload(Client.goals))
        .filter(Client.id == client_id)
        .first()
    )
    if not client:
        return None

    week_budget = weekly_budget(float(client.monthly_budget or 0))
    selected_goals = [g for g in client.goals if g.is_selected]
    if not selected_goals:
        empty_report = ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=0,
            total_clicks=0,
            total_impressions=0,
            total_conversions=0,
            campaigns=(),
            error="Не выбраны цели",
        )
        empty_placements = RsyaPlacementsReport(
            client=client,
            placements=(),
            total_wasted_spend=0.0,
            error="Не выбраны цели — выберите цели для расчёта конверсий по площадкам.",
        )
        return ClientAnalyticsBundle(report=empty_report, placements=empty_placements)

    goal_ids = [g.goal_id for g in selected_goals]
    api = YandexDirectClient(settings.yandex_token, client.yandex_login)

    def _load_campaigns():
        return api.fetch_period_stats(
            date_from,
            date_to,
            goal_ids,
            client.attribution_model,
            settings.vat_rate,
        )

    def _load_placements():
        return api.fetch_rsya_placement_report(
            date_from,
            date_to,
            goal_ids,
            client.attribution_model,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            stats_future = executor.submit(_load_campaigns)
            placements_future = executor.submit(_load_placements)
            daily = stats_future.result()
            raw_placements = placements_future.result()

        campaigns = _aggregate_campaigns(daily)
        report = ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=sum(c.spend for c in campaigns),
            total_clicks=sum(c.clicks for c in campaigns),
            total_impressions=sum(c.impressions for c in campaigns),
            total_conversions=sum(c.conversions for c in campaigns),
            campaigns=tuple(campaigns),
        )
        placements = _filter_zero_conversion_placements(
            raw_placements,
            goal_ids,
            client.attribution_model,
            settings.vat_rate,
            min_clicks=min_clicks,
            min_spend=min_spend,
        )
        placements_report = RsyaPlacementsReport(
            client=client,
            placements=tuple(placements),
            total_wasted_spend=sum(row.spend for row in placements),
        )
        return ClientAnalyticsBundle(report=report, placements=placements_report)
    except Exception as exc:
        error = str(exc)[:400]
        report = ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=0,
            total_clicks=0,
            total_impressions=0,
            total_conversions=0,
            campaigns=(),
            error=error[:300],
        )
        placements_report = RsyaPlacementsReport(
            client=client,
            placements=(),
            total_wasted_spend=0.0,
            error=error,
        )
        return ClientAnalyticsBundle(report=report, placements=placements_report)


def fetch_client_analytics_bundle_cached(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
    *,
    min_clicks: int = 1,
    min_spend: float = 0.0,
) -> ClientAnalyticsBundle | None:
    key = (
        "client_analytics_bundle",
        settings.yandex_token,
        client_id,
        date_from.isoformat(),
        date_to.isoformat(),
        min_clicks,
        round(min_spend, 2),
    )
    return get_or_set(
        key,
        lambda: fetch_client_analytics_bundle(
            db,
            settings,
            client_id,
            date_from,
            date_to,
            min_clicks=min_clicks,
            min_spend=min_spend,
        ),
        ttl_seconds=CACHE_TTL_SECONDS,
    )
