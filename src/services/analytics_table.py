from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.client_balances import ClientBalance, fetch_client_balances
from src.services.budget_pacing import build_budget_pacing, period_day_count
from src.services.cpa_style import weekly_budget
from src.services.direct_report_rows import CACHE_TTL_SECONDS, fetch_campaign_performance_rows_cached
from src.services.parallel_fetch import map_parallel
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import conversions_for_goal, _parse_float, _parse_int


@dataclass(frozen=True)
class AnalyticsRow:
    client_id: int
    client_name: str
    monthly_budget: float
    weekly_budget: float
    spend: float
    impressions: int
    clicks: int
    cpc: float | None
    goal_name: str
    goal_id: int
    conversions: float
    cpa: float | None
    balance: ClientBalance | None = None
    show_client_block: bool = False
    error: str | None = None


def fetch_analytics_table(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[AnalyticsRow]:
    query = db.query(Client).options(joinedload(Client.goals)).order_by(Client.name)
    if active_only:
        query = query.filter(Client.is_active.is_(True))
    clients = query.all()

    balances = fetch_client_balances(
        settings.yandex_token,
        [client.yandex_login for client in clients],
    )

    def _load_client(client: Client) -> list[AnalyticsRow]:
        balance = balances.get(client.yandex_login)
        return _analytics_rows_for_client(client, balance, settings, date_from, date_to)

    client_rows = map_parallel(_load_client, clients)
    return [row for rows in client_rows for row in rows]


def fetch_analytics_table_cached(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    active_only: bool = True,
) -> list[AnalyticsRow]:
    key = (
        "analytics_table",
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        active_only,
    )
    return get_or_set(
        key,
        lambda: fetch_analytics_table(db, settings, date_from, date_to, active_only),
        ttl_seconds=CACHE_TTL_SECONDS,
    )


def _analytics_rows_for_client(
    client: Client,
    balance: ClientBalance | None,
    settings: Settings,
    date_from: date,
    date_to: date,
) -> list[AnalyticsRow]:
    month_budget = float(client.monthly_budget or 0)
    week_budget = weekly_budget(month_budget)
    selected_goals = [g for g in client.goals if g.is_selected]

    if not selected_goals:
        return [
            AnalyticsRow(
                client_id=client.id,
                client_name=client.name,
                monthly_budget=month_budget,
                weekly_budget=week_budget,
                spend=0,
                impressions=0,
                clicks=0,
                cpc=None,
                goal_name="—",
                goal_id=0,
                conversions=0,
                cpa=None,
                balance=balance,
                show_client_block=True,
                error="Не выбраны цели",
            )
        ]

    goal_ids = [g.goal_id for g in selected_goals]
    try:
        raw_rows = fetch_campaign_performance_rows_cached(settings, client, date_from, date_to)
        spend, impressions, clicks, conversions_by_goal, cost_raw = _aggregate_rows(
            raw_rows, goal_ids, client.attribution_model, settings.vat_rate
        )
        cpc = (spend / clicks) if clicks > 0 else None
        rows: list[AnalyticsRow] = []
        for index, goal in enumerate(selected_goals):
            conv = conversions_by_goal.get(goal.goal_id, 0.0)
            cpa = (spend / conv) if conv > 0 else None
            rows.append(
                AnalyticsRow(
                    client_id=client.id,
                    client_name=client.name,
                    monthly_budget=month_budget,
                    weekly_budget=week_budget,
                    spend=spend,
                    impressions=impressions,
                    clicks=clicks,
                    cpc=cpc,
                    goal_name=goal.goal_name,
                    goal_id=goal.goal_id,
                    conversions=conv,
                    cpa=cpa,
                    balance=balance,
                    show_client_block=index == 0,
                )
            )
        return rows
    except Exception as exc:
        return [
            AnalyticsRow(
                client_id=client.id,
                client_name=client.name,
                monthly_budget=month_budget,
                weekly_budget=week_budget,
                spend=0,
                impressions=0,
                clicks=0,
                cpc=None,
                goal_name="—",
                goal_id=0,
                conversions=0,
                cpa=None,
                balance=balance,
                show_client_block=True,
                error=str(exc)[:300],
            )
        ]


def _aggregate_rows(rows, goal_ids, attribution_model, vat_rate):
    cost_raw = 0.0
    impressions = 0
    clicks = 0
    conversions: dict[int, float] = {gid: 0.0 for gid in goal_ids}

    for row in rows:
        cost_raw += _parse_float(row.get("Cost", "0"))
        impressions += _parse_int(row.get("Impressions", "0"))
        clicks += _parse_int(row.get("Clicks", "0"))
        for gid in goal_ids:
            conversions[gid] += conversions_for_goal(row, gid, attribution_model)

    spend = cost_with_vat(cost_raw, vat_rate)
    return spend, impressions, clicks, conversions, cost_raw


SUMMARY_BUDGET_ALERT_PERCENT = 20.0


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_money_summary(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_int_summary(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _budget_status_emoji(deviation_percent: float | None, has_budget: bool) -> str:
    if not has_budget or deviation_percent is None:
        return ""
    if deviation_percent > SUMMARY_BUDGET_ALERT_PERCENT:
        return " 🔴"
    if deviation_percent > 10:
        return " 🟡"
    if deviation_percent >= -10:
        return " ✅"
    return " 🔵"


def format_analytics_telegram(rows: list[AnalyticsRow], date_from: date, date_to: date) -> str:
    period = date_from.strftime("%d.%m.%Y")
    if date_from != date_to:
        period += f" — {date_to.strftime('%d.%m.%Y')}"

    days = period_day_count(date_from, date_to)
    budget_alerts: list[str] = []
    client_blocks: list[str] = []

    index = 0
    while index < len(rows):
        row = rows[index]
        if not row.show_client_block:
            index += 1
            continue

        if row.error:
            client_blocks.append(f"⚠️ <b>{_escape_html(row.client_name)}</b>\n   {_escape_html(row.error)}")
            index += 1
            continue

        goals: list[AnalyticsRow] = []
        next_index = index + 1
        while next_index < len(rows) and not rows[next_index].show_client_block:
            goal_row = rows[next_index]
            if not goal_row.error and goal_row.goal_name != "—":
                goals.append(goal_row)
            next_index += 1

        pacing = build_budget_pacing(row.monthly_budget, row.spend, date_from, date_to)
        spend_text = _fmt_money_summary(row.spend)
        status = _budget_status_emoji(pacing.deviation_percent, pacing.has_budget)

        if pacing.has_budget:
            if days == 1:
                plan_label = f"план {_fmt_money_summary(pacing.daily_budget)} ₽/день"
            else:
                plan_label = f"план {_fmt_money_summary(pacing.expected_spend)} ₽ за {days} дн."
            budget_line = f"💰 <b>{spend_text} ₽</b> · {plan_label}{status}"
        else:
            budget_line = f"💰 <b>{spend_text} ₽</b>"

        if (
            pacing.has_budget
            and pacing.deviation_percent is not None
            and pacing.deviation_percent > SUMMARY_BUDGET_ALERT_PERCENT
        ):
            if days == 1:
                plan_text = f"{_fmt_money_summary(pacing.daily_budget)} ₽/день"
            else:
                plan_text = f"{_fmt_money_summary(pacing.expected_spend)} ₽ за {days} дн."
            deviation = f"+{pacing.deviation_percent:.0f}%"
            budget_alerts.append(
                f"• <b>{_escape_html(row.client_name)}</b>: "
                f"расход {spend_text} ₽ при {plan_text} ({deviation})"
            )

        block_lines = [
            f"<b>{_escape_html(row.client_name)}</b>",
            budget_line,
            f"👁 {_fmt_int_summary(row.impressions)} · 👆 {_fmt_int_summary(row.clicks)} кл.",
        ]
        if row.monthly_budget > 0:
            block_lines.append(
                f"📋 бюджет/мес {_fmt_money_summary(row.monthly_budget)} ₽ · "
                f"нед {_fmt_money_summary(row.weekly_budget)} ₽"
            )
        for goal in goals:
            cpa_text = _fmt_money_summary(goal.cpa) if goal.cpa is not None else "—"
            conv_text = f"{goal.conversions:g}".replace(".", ",")
            block_lines.append(
                f"   ↳ {_escape_html(goal.goal_name)}: {conv_text} конв. · CPA {cpa_text} ₽"
            )

        client_blocks.append("\n".join(block_lines))
        index = next_index

    lines = [
        "📊 <b>Сводка Direct Nikitos Analytics</b>",
        f"📅 {period}",
    ]

    if budget_alerts:
        lines.extend(
            [
                "",
                f"🚨 <b>Превышение бюджета (&gt;{int(SUMMARY_BUDGET_ALERT_PERCENT)}%)</b>",
                *budget_alerts,
            ]
        )

    if client_blocks:
        lines.extend(["", "─────────────────", ""])
        lines.append("\n\n".join(client_blocks))

    if not client_blocks and not budget_alerts:
        lines.append("")
        lines.append("Нет данных по клиентам.")

    return "\n".join(lines)
