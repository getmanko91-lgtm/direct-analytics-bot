from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.analytics_table import fetch_analytics_table_cached
from src.services.client_balances import BALANCE_LOW_THRESHOLD, fetch_client_balances
from src.services.cpa_style import weekly_budget
from src.services.direct_report_rows import fetch_campaign_performance_rows_cached
from src.services.parallel_fetch import map_parallel
from src.services.rsya_placements import _filter_zero_conversion_placements
from src.vat import cost_with_vat
from src.yandex_direct import (
    YandexDirectClient,
    _parse_float,
    conversions_for_goal,
)

DEAD_CAMPAIGN_MIN_SPEND = 2_000.0
RSYA_MIN_SPEND = 600.0
CHEAP_CPA_MAX = 200.0


@dataclass(frozen=True)
class WeeklyOverspend:
    client_name: str
    directologist: str
    spend: float
    plan: float
    deviation_percent: float


@dataclass(frozen=True)
class CampaignAlert:
    client_name: str
    directologist: str
    campaign_name: str
    spend: float
    conversions: float
    cpa: float | None


@dataclass(frozen=True)
class RsyaWasteAlert:
    client_name: str
    directologist: str
    placement: str
    spend: float


@dataclass(frozen=True)
class BalanceAlert:
    client_name: str
    directologist: str
    amount: float


@dataclass
class OperatorAlerts:
    weekly_overspend: list[WeeklyOverspend] = field(default_factory=list)
    dead_campaigns: list[CampaignAlert] = field(default_factory=list)
    rsya_waste: list[RsyaWasteAlert] = field(default_factory=list)
    cheap_campaigns: list[CampaignAlert] = field(default_factory=list)
    low_balance: list[BalanceAlert] = field(default_factory=list)
    zero_balance: list[BalanceAlert] = field(default_factory=list)
    week_from: date | None = None
    week_to: date | None = None
    bidaily_from: date | None = None
    bidaily_to: date | None = None


def calendar_week_containing(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def last_complete_days(yesterday: date, days: int = 2) -> tuple[date, date]:
    start = yesterday - timedelta(days=days - 1)
    return start, yesterday


def is_weekly_alert_day(today: date) -> bool:
    return today.weekday() == 0


def is_bidaily_alert_day(today: date) -> bool:
    return today.toordinal() % 2 == 0


def aggregate_campaigns(
    rows: list[dict[str, str]],
    goal_ids: list[int],
    attribution_model: str,
    vat_rate: float,
) -> list[tuple[str, float, float, float | None]]:
    buckets: dict[str, dict[str, float | str]] = {}
    for row in rows:
        name = (row.get("CampaignName") or "").strip() or "—"
        campaign_id = (row.get("CampaignId") or "").strip()
        key = campaign_id or name
        if key not in buckets:
            buckets[key] = {"name": name, "cost_raw": 0.0, "conversions": 0.0}
        buckets[key]["cost_raw"] = float(buckets[key]["cost_raw"]) + _parse_float(row.get("Cost", "0"))
        conversions = sum(conversions_for_goal(row, gid, attribution_model) for gid in goal_ids)
        buckets[key]["conversions"] = float(buckets[key]["conversions"]) + conversions

    result: list[tuple[str, float, float, float | None]] = []
    for item in buckets.values():
        spend = cost_with_vat(float(item["cost_raw"]), vat_rate)
        conversions = float(item["conversions"])
        cpa = (spend / conversions) if conversions > 0 else None
        result.append((str(item["name"]), spend, conversions, cpa))
    result.sort(key=lambda item: item[1], reverse=True)
    return result


def filter_dead_campaigns(
    campaigns: list[tuple[str, float, float, float | None]],
    *,
    min_spend: float = DEAD_CAMPAIGN_MIN_SPEND,
) -> list[tuple[str, float, float, float | None]]:
    return [
        item
        for item in campaigns
        if item[1] >= min_spend and item[2] <= 0
    ]


def filter_cheap_campaigns(
    campaigns: list[tuple[str, float, float, float | None]],
    *,
    max_cpa: float = CHEAP_CPA_MAX,
) -> list[tuple[str, float, float, float | None]]:
    return [
        item
        for item in campaigns
        if item[3] is not None and 0 < item[3] < max_cpa
    ]


def split_balance_alerts(
    clients: list[Client],
    balances: dict,
    *,
    low_max: float = BALANCE_LOW_THRESHOLD,
) -> tuple[list[BalanceAlert], list[BalanceAlert]]:
    zero: list[BalanceAlert] = []
    low: list[BalanceAlert] = []
    for client in clients:
        balance = balances.get(client.yandex_login)
        if balance is None or balance.amount is None:
            continue
        amount = float(balance.amount)
        item = BalanceAlert(
            client_name=client.name,
            directologist=client.directologist or "—",
            amount=amount,
        )
        if amount <= 0:
            zero.append(item)
        elif amount <= low_max:
            low.append(item)
    return zero, low


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_period(date_from: date, date_to: date) -> str:
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")
    return f"{date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"


def _header(title: str, period: str | None = None) -> list[str]:
    lines = ["Операторам", "", title]
    if period:
        lines.append(period)
    return lines


def _client_label(item) -> str:
    return f"{_escape(item.directologist)} · {_escape(item.client_name)}"


def format_weekly_overspend(alerts: list[WeeklyOverspend], date_from: date, date_to: date) -> str:
    lines = _header("Перекрут недельного бюджета", _fmt_period(date_from, date_to))
    lines.append("")
    for item in alerts:
        lines.append(_client_label(item))
        lines.append(
            f"расход {_fmt_money(item.spend)} ₽ при недельном бюджете {_fmt_money(item.plan)} ₽ "
            f"(+{item.deviation_percent:.0f}%)"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def format_dead_campaigns(alerts: list[CampaignAlert], date_from: date, date_to: date) -> str:
    lines = _header(
        f"Кампания открутила от {_fmt_money(DEAD_CAMPAIGN_MIN_SPEND)} ₽ и не дала конверсий",
        _fmt_period(date_from, date_to),
    )
    lines.append("")
    for item in alerts:
        lines.append(_client_label(item))
        lines.append(
            f"{_escape(item.campaign_name)} — {_fmt_money(item.spend)} ₽, 0 конверсий"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def format_rsya_waste(alerts: list[RsyaWasteAlert], date_from: date, date_to: date) -> str:
    lines = _header(
        f"Площадка РСЯ съела больше {_fmt_money(RSYA_MIN_SPEND)} ₽ без конверсий",
        _fmt_period(date_from, date_to),
    )
    lines.append("")
    for item in alerts:
        lines.append(_client_label(item))
        lines.append(f"{_escape(item.placement)} — {_fmt_money(item.spend)} ₽, 0 конверсий")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_cheap_campaigns(alerts: list[CampaignAlert], date_from: date, date_to: date) -> str:
    lines = _header(
        f"Аномально дешёвые кампании (CPA ниже {_fmt_money(CHEAP_CPA_MAX)} ₽)",
        _fmt_period(date_from, date_to),
    )
    lines.append("")
    for item in alerts:
        conv = f"{item.conversions:g}".replace(".", ",")
        lines.append(_client_label(item))
        lines.append(
            f"{_escape(item.campaign_name)} — CPA {_fmt_money(item.cpa or 0)} ₽ "
            f"({conv} конв., расход {_fmt_money(item.spend)} ₽)"
        )
        lines.append("")
    return "\n".join(lines).rstrip()


def format_low_balance(alerts: list[BalanceAlert]) -> str:
    lines = _header(f"На балансе осталось {_fmt_money(BALANCE_LOW_THRESHOLD)} ₽ или меньше")
    lines.append("")
    for item in alerts:
        lines.append(f"{_client_label(item)} — {_fmt_money(item.amount)} ₽")
    return "\n".join(lines)


def format_zero_balance(alerts: list[BalanceAlert]) -> str:
    lines = _header("На балансе 0 ₽")
    lines.append("")
    for item in alerts:
        lines.append(f"{_client_label(item)} — {_fmt_money(item.amount)} ₽")
    return "\n".join(lines)


def format_operator_messages(alerts: OperatorAlerts) -> list[str]:
    messages: list[str] = []
    if alerts.weekly_overspend and alerts.week_from and alerts.week_to:
        messages.append(format_weekly_overspend(alerts.weekly_overspend, alerts.week_from, alerts.week_to))
    if alerts.dead_campaigns and alerts.bidaily_from and alerts.bidaily_to:
        messages.append(format_dead_campaigns(alerts.dead_campaigns, alerts.bidaily_from, alerts.bidaily_to))
    if alerts.rsya_waste and alerts.week_from and alerts.week_to:
        messages.append(format_rsya_waste(alerts.rsya_waste, alerts.week_from, alerts.week_to))
    if alerts.cheap_campaigns and alerts.week_from and alerts.week_to:
        messages.append(format_cheap_campaigns(alerts.cheap_campaigns, alerts.week_from, alerts.week_to))
    if alerts.low_balance:
        messages.append(format_low_balance(alerts.low_balance))
    if alerts.zero_balance:
        messages.append(format_zero_balance(alerts.zero_balance))
    return messages


def _clients_with_goals(db: Session) -> list[Client]:
    return (
        db.query(Client)
        .options(joinedload(Client.goals))
        .filter(Client.is_active.is_(True))
        .order_by(Client.name)
        .all()
    )


def collect_operator_alerts(
    db: Session,
    settings: Settings,
    today: date,
) -> OperatorAlerts:
    yesterday = today - timedelta(days=1)
    clients = _clients_with_goals(db)
    clients_by_id = {client.id: client for client in clients}
    result = OperatorAlerts()

    balances = fetch_client_balances(settings.yandex_token, [client.yandex_login for client in clients])
    result.zero_balance, result.low_balance = split_balance_alerts(clients, balances)

    if is_weekly_alert_day(today):
        week_from, week_to = calendar_week_containing(yesterday)
        result.week_from = week_from
        result.week_to = week_to
        result.weekly_overspend = _weekly_overspend(db, settings, clients_by_id, week_from, week_to)
        result.rsya_waste = _rsya_waste(settings, clients, week_from, week_to)
        result.cheap_campaigns = _campaign_alerts(
            settings,
            clients,
            week_from,
            week_to,
            kind="cheap",
        )

    if is_bidaily_alert_day(today):
        bidaily_from, bidaily_to = last_complete_days(yesterday, 2)
        result.bidaily_from = bidaily_from
        result.bidaily_to = bidaily_to
        result.dead_campaigns = _campaign_alerts(
            settings,
            clients,
            bidaily_from,
            bidaily_to,
            kind="dead",
        )

    return result


def collect_operator_alert_messages(
    db: Session,
    settings: Settings,
    today: date,
) -> list[str]:
    return format_operator_messages(collect_operator_alerts(db, settings, today))


def _weekly_overspend(
    db: Session,
    settings: Settings,
    clients_by_id: dict[int, Client],
    week_from: date,
    week_to: date,
) -> list[WeeklyOverspend]:
    rows = fetch_analytics_table_cached(db, settings, week_from, week_to)
    alerts: list[WeeklyOverspend] = []
    seen: set[int] = set()
    for row in rows:
        if not row.show_client_block or row.client_id in seen or row.error:
            continue
        seen.add(row.client_id)
        plan = weekly_budget(row.monthly_budget)
        if plan <= 0 or row.spend <= plan:
            continue
        client = clients_by_id.get(row.client_id)
        deviation = ((row.spend - plan) / plan) * 100
        alerts.append(
            WeeklyOverspend(
                client_name=row.client_name,
                directologist=(client.directologist if client else "") or "—",
                spend=row.spend,
                plan=plan,
                deviation_percent=deviation,
            )
        )
    return alerts


def _campaign_alerts(
    settings: Settings,
    clients: list[Client],
    date_from: date,
    date_to: date,
    *,
    kind: str,
) -> list[CampaignAlert]:
    ready = [client for client in clients if any(goal.is_selected for goal in client.goals)]

    def _for_client(client: Client) -> list[CampaignAlert]:
        goal_ids = [goal.goal_id for goal in client.goals if goal.is_selected]
        try:
            rows = fetch_campaign_performance_rows_cached(settings, client, date_from, date_to)
        except Exception:
            return []
        campaigns = aggregate_campaigns(rows, goal_ids, client.attribution_model, settings.vat_rate)
        picked = filter_dead_campaigns(campaigns) if kind == "dead" else filter_cheap_campaigns(campaigns)
        return [
            CampaignAlert(
                client_name=client.name,
                directologist=client.directologist or "—",
                campaign_name=name,
                spend=spend,
                conversions=conversions,
                cpa=cpa,
            )
            for name, spend, conversions, cpa in picked
        ]

    alerts: list[CampaignAlert] = []
    for batch in map_parallel(_for_client, ready):
        alerts.extend(batch)
    return alerts


def _rsya_waste(
    settings: Settings,
    clients: list[Client],
    date_from: date,
    date_to: date,
) -> list[RsyaWasteAlert]:
    ready = [client for client in clients if any(goal.is_selected for goal in client.goals)]

    def _for_client(client: Client) -> list[RsyaWasteAlert]:
        goal_ids = [goal.goal_id for goal in client.goals if goal.is_selected]
        try:
            api = YandexDirectClient(settings.yandex_token, client.yandex_login)
            raw_rows = api.fetch_rsya_placement_report(
                date_from,
                date_to,
                goal_ids,
                client.attribution_model,
            )
        except Exception:
            return []
        placements = _filter_zero_conversion_placements(
            raw_rows,
            goal_ids,
            client.attribution_model,
            settings.vat_rate,
            min_clicks=0,
            min_spend=0.0,
        )
        return [
            RsyaWasteAlert(
                client_name=client.name,
                directologist=client.directologist or "—",
                placement=row.placement,
                spend=row.spend,
            )
            for row in placements
            if row.spend > RSYA_MIN_SPEND
        ]

    alerts: list[RsyaWasteAlert] = []
    for batch in map_parallel(_for_client, ready):
        alerts.extend(batch)
    return alerts
