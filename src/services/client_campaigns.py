from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.cpa_style import cpa_highlight_class, weekly_budget
from src.services.direct_report_rows import CACHE_TTL_SECONDS
from src.services.runtime_cache import get_or_set
from src.yandex_direct import DailyStats, YandexDirectClient


@dataclass(frozen=True)
class ClientCampaignRow:
    campaign_name: str
    spend: float
    impressions: int
    clicks: int
    cpc: float | None
    conversions: float
    cpa: float | None
    cpa_class: str = ""


@dataclass(frozen=True)
class ClientCampaignReport:
    client: Client
    weekly_budget: float
    total_spend: float
    total_clicks: int
    total_impressions: int
    total_conversions: float
    campaigns: tuple[ClientCampaignRow, ...]
    error: str | None = None


def fetch_client_campaign_report(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
) -> ClientCampaignReport | None:
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
        return ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=0,
            total_clicks=0,
            total_impressions=0,
            total_conversions=0,
            campaigns=(),
            error="Не выбраны цели",
        )

    goal_ids = [g.goal_id for g in selected_goals]
    try:
        api = YandexDirectClient(settings.yandex_token, client.yandex_login)
        daily = api.fetch_period_stats(
            date_from,
            date_to,
            goal_ids,
            client.attribution_model,
            settings.vat_rate,
        )
        campaigns = _aggregate_campaigns(daily)
        return ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=sum(c.spend for c in campaigns),
            total_clicks=sum(c.clicks for c in campaigns),
            total_impressions=sum(c.impressions for c in campaigns),
            total_conversions=sum(c.conversions for c in campaigns),
            campaigns=tuple(campaigns),
        )
    except Exception as exc:
        return ClientCampaignReport(
            client=client,
            weekly_budget=week_budget,
            total_spend=0,
            total_clicks=0,
            total_impressions=0,
            total_conversions=0,
            campaigns=(),
            error=str(exc)[:300],
        )


def fetch_client_campaign_report_cached(
    db: Session,
    settings: Settings,
    client_id: int,
    date_from: date,
    date_to: date,
) -> ClientCampaignReport | None:
    key = (
        "client_campaign_report",
        settings.yandex_token,
        client_id,
        date_from.isoformat(),
        date_to.isoformat(),
    )
    return get_or_set(
        key,
        lambda: fetch_client_campaign_report(db, settings, client_id, date_from, date_to),
        ttl_seconds=CACHE_TTL_SECONDS,
    )


def _aggregate_campaigns(daily: dict[date, DailyStats]) -> list[ClientCampaignRow]:
    merged: dict[str, dict[str, float | int]] = {}
    for day_stats in daily.values():
        for campaign in day_stats.campaigns:
            bucket = merged.setdefault(
                campaign.campaign_name,
                {"spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0},
            )
            bucket["spend"] = float(bucket["spend"]) + campaign.cost
            bucket["impressions"] = int(bucket["impressions"]) + campaign.impressions
            bucket["clicks"] = int(bucket["clicks"]) + campaign.clicks
            bucket["conversions"] = float(bucket["conversions"]) + campaign.conversions

    rows: list[ClientCampaignRow] = []
    for name, stats in merged.items():
        spend = float(stats["spend"])
        clicks = int(stats["clicks"])
        conversions = float(stats["conversions"])
        cpc = spend / clicks if clicks > 0 else None
        cpa = spend / conversions if conversions > 0 else None
        rows.append(
            ClientCampaignRow(
                campaign_name=name,
                spend=spend,
                impressions=int(stats["impressions"]),
                clicks=clicks,
                cpc=cpc,
                conversions=conversions,
                cpa=cpa,
                cpa_class=cpa_highlight_class(cpa),
            )
        )
    rows.sort(key=lambda row: row.spend, reverse=True)
    return rows
