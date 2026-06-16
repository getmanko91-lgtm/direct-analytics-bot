from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.vat import cost_with_vat
from src.yandex_direct import (
    YandexDirectClient,
    _conversion_field,
    _parse_float,
    _parse_int,
)


@dataclass(frozen=True)
class AnalyticsRow:
    client_id: int
    client_name: str
    spend: float
    impressions: int
    goal_name: str
    goal_id: int
    conversions: float
    cpa: float | None
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

    rows: list[AnalyticsRow] = []
    for client in clients:
        selected_goals = [g for g in client.goals if g.is_selected]
        if not selected_goals:
            rows.append(
                AnalyticsRow(
                    client_id=client.id,
                    client_name=client.name,
                    spend=0,
                    impressions=0,
                    goal_name="—",
                    goal_id=0,
                    conversions=0,
                    cpa=None,
                    error="Не выбраны цели",
                )
            )
            continue

        goal_ids = [g.goal_id for g in selected_goals]
        goal_names = {g.goal_id: g.goal_name for g in selected_goals}

        try:
            api = YandexDirectClient(settings.yandex_token, client.yandex_login)
            raw_rows = _fetch_all_report_rows(
                api, date_from, date_to, goal_ids, client.attribution_model
            )
            spend, impressions, conversions_by_goal = _aggregate_rows(
                raw_rows, goal_ids, client.attribution_model, settings.vat_rate
            )
            for goal in selected_goals:
                conv = conversions_by_goal.get(goal.goal_id, 0.0)
                cpa = (spend / conv) if conv > 0 else None
                rows.append(
                    AnalyticsRow(
                        client_id=client.id,
                        client_name=client.name,
                        spend=spend,
                        impressions=impressions,
                        goal_name=goal.goal_name,
                        goal_id=goal.goal_id,
                        conversions=conv,
                        cpa=cpa,
                    )
                )
        except Exception as exc:
            rows.append(
                AnalyticsRow(
                    client_id=client.id,
                    client_name=client.name,
                    spend=0,
                    impressions=0,
                    goal_name="—",
                    goal_id=0,
                    conversions=0,
                    cpa=None,
                    error=str(exc)[:300],
                )
            )
    return rows


def _fetch_all_report_rows(api, date_from, date_to, goal_ids, attribution_model):
    from src.yandex_direct import MAX_GOALS_PER_REQUEST, _chunked

    if not goal_ids:
        return api._fetch_report(date_from, date_to, [], attribution_model)

    merged: dict[tuple[str, str], dict[str, str]] = {}
    for chunk in _chunked(goal_ids, MAX_GOALS_PER_REQUEST):
        chunk_rows = api._fetch_report(date_from, date_to, list(chunk), attribution_model)
        for row in chunk_rows:
            key = (row.get("Date", ""), row.get("CampaignName", "—"))
            if key not in merged:
                merged[key] = dict(row)
            else:
                target = merged[key]
                for gid in chunk:
                    conv_key = _conversion_field(gid, attribution_model)
                    if conv_key in row:
                        cur = _parse_float(target.get(conv_key, "0"))
                        target[conv_key] = str(cur + _parse_float(row.get(conv_key, "0")))
    return list(merged.values())


def _aggregate_rows(rows, goal_ids, attribution_model, vat_rate):
    cost_raw = 0.0
    impressions = 0
    conversions: dict[int, float] = {gid: 0.0 for gid in goal_ids}

    for row in rows:
        cost_raw += _parse_float(row.get("Cost", "0"))
        impressions += _parse_int(row.get("Impressions", "0"))
        for gid in goal_ids:
            key = _conversion_field(gid, attribution_model)
            conversions[gid] += _parse_float(row.get(key, "0"))

    spend = cost_with_vat(cost_raw, vat_rate)
    return spend, impressions, conversions


def format_analytics_telegram(rows: list[AnalyticsRow], date_from: date, date_to: date) -> str:
    period = date_from.strftime("%d.%m.%Y")
    if date_from != date_to:
        period += f" — {date_to.strftime('%d.%m.%Y')}"

    lines = [f"📊 Сводка Direct Analytics", f"Период: {period}", ""]
    for row in rows:
        if row.error:
            lines.append(f"• {row.client_name}: ⚠️ {row.error}")
            continue
        cpa_text = f"{row.cpa:,.2f} ₽".replace(",", " ").replace(".", ",") if row.cpa else "—"
        spend_text = f"{row.spend:,.2f} ₽".replace(",", " ").replace(".", ",")
        lines.append(
            f"• {row.client_name}\n"
            f"  Расход: {spend_text} | Показы: {row.impressions:,}".replace(",", " ")
            + f"\n  {row.goal_name}: {row.conversions:g} конв. | CPA: {cpa_text}"
        )
    return "\n".join(lines)
