from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.client_balances import ClientBalance, fetch_client_balances
from src.services.cpa_style import cpa_highlight_class, weekly_budget
from src.vat import cost_with_vat
from src.yandex_direct import (
    YandexDirectClient,
    _merge_conversion_columns,
    _report_row_key,
    conversions_for_goal,
    _parse_float,
    _parse_int,
)


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
    client_block_shown: set[int] = set()

    rows: list[AnalyticsRow] = []
    for client in clients:
        balance = balances.get(client.yandex_login)
        month_budget = float(client.monthly_budget or 0)
        week_budget = weekly_budget(month_budget)
        selected_goals = [g for g in client.goals if g.is_selected]

        def _show_block() -> bool:
            return client.id not in client_block_shown

        def _mark_shown() -> None:
            client_block_shown.add(client.id)

        if not selected_goals:
            rows.append(
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
                    show_client_block=_show_block(),
                    error="Не выбраны цели",
                )
            )
            _mark_shown()
            continue

        goal_ids = [g.goal_id for g in selected_goals]

        try:
            api = YandexDirectClient(settings.yandex_token, client.yandex_login)
            raw_rows = _fetch_all_report_rows(
                api, date_from, date_to, goal_ids, client.attribution_model
            )
            spend, impressions, clicks, conversions_by_goal, cost_raw = _aggregate_rows(
                raw_rows, goal_ids, client.attribution_model, settings.vat_rate
            )
            cpc = (spend / clicks) if clicks > 0 else None
            for goal in selected_goals:
                conv = conversions_by_goal.get(goal.goal_id, 0.0)
                cpa = (cost_raw / conv) if conv > 0 else None
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
                        show_client_block=_show_block(),
                    )
                )
                _mark_shown()
        except Exception as exc:
            rows.append(
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
                    show_client_block=_show_block(),
                    error=str(exc)[:300],
                )
            )
            _mark_shown()
    return rows


def _fetch_all_report_rows(api, date_from, date_to, goal_ids, attribution_model):
    from src.yandex_direct import MAX_GOALS_PER_REQUEST, _chunked

    if not goal_ids:
        return api._fetch_report(date_from, date_to, [], attribution_model)

    merged: dict[tuple[str, str], dict[str, str]] = {}
    for chunk in _chunked(goal_ids, MAX_GOALS_PER_REQUEST):
        chunk_rows = api._fetch_report(date_from, date_to, list(chunk), attribution_model)
        for row in chunk_rows:
            key = _report_row_key(row)
            if key not in merged:
                merged[key] = dict(row)
            else:
                target = merged[key]
                _merge_conversion_columns(target, row, list(chunk))
    return list(merged.values())


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


def format_analytics_telegram(rows: list[AnalyticsRow], date_from: date, date_to: date) -> str:
    period = date_from.strftime("%d.%m.%Y")
    if date_from != date_to:
        period += f" — {date_to.strftime('%d.%m.%Y')}"

    lines = [f"📊 Сводка Direct Analytics", f"Период: {period}", ""]
    for row in rows:
        if row.error and row.show_client_block:
            lines.append(f"• {row.client_name}: ⚠️ {row.error}")
            continue
        if row.show_client_block:
            spend_text = f"{row.spend:,.2f} ₽".replace(",", " ").replace(".", ",")
            budget_text = f"{row.monthly_budget:,.0f} ₽".replace(",", " ") if row.monthly_budget else "—"
            lines.append(
                f"• {row.client_name}\n"
                f"  Бюджет/мес: {budget_text} | Расход: {spend_text} | "
                f"Показы: {row.impressions:,} | Клики: {row.clicks}".replace(",", " ")
            )
        if row.error:
            continue
        if row.goal_name == "—":
            continue
        cpa_text = f"{row.cpa:,.2f} ₽".replace(",", " ").replace(".", ",") if row.cpa else "—"
        lines.append(f"  ↳ {row.goal_name}: {row.conversions:g} конв. | CPA: {cpa_text}")
    return "\n".join(lines)
