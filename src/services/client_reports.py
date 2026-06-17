from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.direct_report_rows import CACHE_TTL_SECONDS, fetch_client_report_rows_cached
from src.services.parallel_fetch import map_parallel
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import (
    _parse_date,
    _parse_float,
    conversions_for_goal,
)


def client_report_category(campaign_name: str) -> str | None:
    upper = campaign_name.strip().upper()
    if upper.startswith("КОНВЕРСИИ") or upper.startswith("КОНВЕРС"):
        return "conversion"
    if upper.startswith("ИМИДЖ"):
        return "image"
    if upper.startswith("ПРИЛОЖ") or upper.startswith("ПРИЛ"):
        return "app"
    return None


def iter_week_ranges(date_from: date, date_to: date) -> list[tuple[date, date]]:
    weeks: list[tuple[date, date]] = []
    current = date_from
    while current <= date_to:
        week_end = min(current + timedelta(days=6), date_to)
        weeks.append((current, week_end))
        current = week_end + timedelta(days=1)
    return weeks


def _week_index(day: date, weeks: list[tuple[date, date]]) -> int | None:
    for index, (week_from, week_to) in enumerate(weeks):
        if week_from <= day <= week_to:
            return index
    return None


@dataclass
class WeekMetrics:
    conv_spend_raw: float = 0.0
    conv_count: float = 0.0
    image_spend_raw: float = 0.0
    image_impressions: int = 0
    image_conversions: float = 0.0
    app_spend_raw: float = 0.0
    app_installs: float = 0.0
    app_revenue_raw: float = 0.0

    def total_spend_raw(self) -> float:
        return self.conv_spend_raw + self.image_spend_raw + self.app_spend_raw

    def add(self, other: WeekMetrics) -> None:
        self.conv_spend_raw += other.conv_spend_raw
        self.conv_count += other.conv_count
        self.image_spend_raw += other.image_spend_raw
        self.image_impressions += other.image_impressions
        self.image_conversions += other.image_conversions
        self.app_spend_raw += other.app_spend_raw
        self.app_installs += other.app_installs
        self.app_revenue_raw += other.app_revenue_raw


@dataclass
class ClientMonthlyReport:
    client_id: int
    client_name: str
    weeks: list[tuple[date, date]] = field(default_factory=list)
    week_metrics: list[WeekMetrics] = field(default_factory=list)
    total: WeekMetrics = field(default_factory=WeekMetrics)
    plan_budget: float = 0.0
    error: str | None = None


def fetch_client_reports(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[ClientMonthlyReport]:
    query = db.query(Client).options(joinedload(Client.goals)).order_by(Client.name)
    if active_only:
        query = query.filter(Client.is_active.is_(True))
    clients = query.all()

    weeks = iter_week_ranges(date_from, date_to)

    def _load_client(client: Client) -> ClientMonthlyReport:
        return _client_report_for_client(client, weeks, settings, date_from, date_to)

    return map_parallel(_load_client, clients)


def _client_report_for_client(
    client: Client,
    weeks: list[tuple[date, date]],
    settings: Settings,
    date_from: date,
    date_to: date,
) -> ClientMonthlyReport:
    report = ClientMonthlyReport(
        client_id=client.id,
        client_name=client.name,
        weeks=weeks,
        week_metrics=[WeekMetrics() for _ in weeks],
        plan_budget=client.monthly_budget or 0.0,
    )
    selected_goals = [g for g in client.goals if g.is_selected]
    if not selected_goals:
        report.error = "Не выбраны цели"
        return report

    goal_ids = [g.goal_id for g in selected_goals]
    try:
        rows = fetch_client_report_rows_cached(settings, client, date_from, date_to)
        _aggregate_rows_into_report(report, rows, goal_ids, client.attribution_model)
    except Exception as exc:
        report.error = str(exc)[:300]
    return report


def fetch_client_reports_cached(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[ClientMonthlyReport]:
    key = (
        "client_reports",
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        active_only,
    )
    return get_or_set(
        key,
        lambda: fetch_client_reports(db, settings, date_from, date_to, active_only),
        ttl_seconds=CACHE_TTL_SECONDS,
    )


def _aggregate_rows_into_report(
    report: ClientMonthlyReport,
    rows: list[dict[str, str]],
    goal_ids: list[int],
    attribution_model: str,
) -> None:
    for row in rows:
        campaign_name = (row.get("CampaignName") or "").strip()
        category = client_report_category(campaign_name)
        if category is None:
            continue

        day = _parse_date(row.get("Date", ""))
        week_idx = _week_index(day, report.weeks)
        if week_idx is None:
            continue

        metrics = report.week_metrics[week_idx]
        cost_raw = _parse_float(row.get("Cost", "0"))
        impressions = int(_parse_float(row.get("Impressions", "0")))
        conversions = sum(conversions_for_goal(row, gid, attribution_model) for gid in goal_ids)
        revenue_raw = _parse_float(row.get("Revenue", "0"))

        if category == "conversion":
            metrics.conv_spend_raw += cost_raw
            metrics.conv_count += conversions
        elif category == "image":
            metrics.image_spend_raw += cost_raw
            metrics.image_impressions += impressions
            metrics.image_conversions += conversions
        elif category == "app":
            metrics.app_spend_raw += cost_raw
            metrics.app_installs += conversions
            metrics.app_revenue_raw += revenue_raw

    report.total = WeekMetrics()
    for week in report.week_metrics:
        report.total.add(week)


def format_period(week_from: date, week_to: date) -> str:
    return f"{week_from.strftime('%d.%m.%Y')} - {week_to.strftime('%d.%m.%Y')}"


def format_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def format_number(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".replace(".", ",")


def format_ratio(numerator: float, denominator: float) -> str:
    if denominator <= 0:
        return "#DIV/0!"
    return format_money(numerator / denominator)


def metrics_to_display(metrics: WeekMetrics, vat_rate: float) -> dict[str, str]:
    conv_spend = cost_with_vat(metrics.conv_spend_raw, vat_rate)
    image_spend = cost_with_vat(metrics.image_spend_raw, vat_rate)
    app_spend = cost_with_vat(metrics.app_spend_raw, vat_rate)
    total_spend = conv_spend + image_spend + app_spend
    app_revenue = cost_with_vat(metrics.app_revenue_raw, vat_rate)

    return {
        "conv_spend": format_money(conv_spend),
        "conv_count": format_number(metrics.conv_count),
        "conv_price": format_ratio(metrics.conv_spend_raw, metrics.conv_count),
        "image_spend": format_money(image_spend),
        "image_impressions": format_number(metrics.image_impressions),
        "image_cpm": format_ratio(metrics.image_spend_raw * 1000, metrics.image_impressions),
        "image_conversions": format_number(metrics.image_conversions),
        "app_spend": format_money(app_spend),
        "app_installs": format_number(metrics.app_installs),
        "app_cpi": format_ratio(metrics.app_spend_raw, metrics.app_installs),
        "app_revenue": format_money(app_revenue),
        "total_spend": format_money(total_spend),
    }
