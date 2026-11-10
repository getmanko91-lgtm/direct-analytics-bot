from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.client_balances import fetch_client_balances
from src.services.direct_report_rows import CACHE_TTL_SECONDS, fetch_campaign_performance_rows_cached
from src.services.parallel_fetch import map_parallel
from src.services.runtime_cache import get_or_set
from src.vat import cost_with_vat
from src.yandex_direct import _parse_float, conversions_for_goal

KPI_CAMPAIGN_PREFIXES = ("КОНВЕР", "МК", "ЕПК", "ТМК")


def is_kpi_campaign(campaign_name: str) -> bool:
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
        if not is_kpi_campaign(campaign_name):
            continue
        spend_raw += _parse_float(row.get("Cost", "0"))
        for gid in goal_ids:
            conversions += conversions_for_goal(row, gid, attribution_model)
    return spend_raw, conversions


@dataclass
class KpiOverrun:
    client_name: str
    cpa: float
    kpi: float


@dataclass
class KpiAlerts:
    no_balance: list[str] = field(default_factory=list)
    kpi_over: list[KpiOverrun] = field(default_factory=list)


def collect_kpi_alerts(
    db: Session,
    settings: Settings,
    date_from: date,
    date_to: date,
    *,
    active_only: bool = True,
) -> KpiAlerts:
    rows = fetch_kpi_table_cached(db, settings, date_from, date_to, active_only)
    query = db.query(Client).order_by(Client.name)
    if active_only:
        query = query.filter(Client.is_active.is_(True))
    clients = query.all()
    clients_by_id = {client.id: client for client in clients}
    balances = fetch_client_balances(
        settings.yandex_token,
        [client.yandex_login for client in clients],
    )

    alerts = KpiAlerts()
    seen_balance: set[int] = set()
    for row in rows:
        client = clients_by_id.get(row.client_id)
        if not client:
            continue
        if client.id not in seen_balance:
            seen_balance.add(client.id)
            balance = balances.get(client.yandex_login)
            if balance and balance.amount is not None and balance.amount <= 0:
                alerts.no_balance.append(client.name)
        if (
            not row.error
            and row.cpa is not None
            and client.manual_kpi is not None
            and client.manual_kpi > 0
            and row.cpa > client.manual_kpi
        ):
            alerts.kpi_over.append(
                KpiOverrun(client_name=client.name, cpa=row.cpa, kpi=client.manual_kpi)
            )
    return alerts


def format_kpi_telegram(
    alerts: KpiAlerts,
    date_from: date,
    date_to: date,
) -> str:
    period = date_from.strftime("%d.%m.%Y")
    if date_from != date_to:
        period += f" — {date_to.strftime('%d.%m.%Y')}"

    lines = [
        "📣 <b>Уведомления Direct Nikitos Analytics</b>",
        f"📅 {period}",
    ]

    if alerts.no_balance:
        lines.extend(
            [
                "",
                "💸 <b>Денег на балансе нет</b>",
                *[f"• <b>{_escape_html(name)}</b>" for name in alerts.no_balance],
            ]
        )

    if alerts.kpi_over:
        lines.extend(
            [
                "",
                "🎯 <b>KPI превышен</b> (CPA выше KPI проекта)",
                *[
                    (
                        f"• <b>{_escape_html(item.client_name)}</b>: "
                        f"CPA {_fmt_money(item.cpa)} ₽ при KPI {_fmt_money(item.kpi)} ₽"
                    )
                    for item in alerts.kpi_over
                ],
            ]
        )

    if not alerts.no_balance and not alerts.kpi_over:
        lines.extend(["", "✅ Нет кабинетов без денег и превышений KPI."])

    return "\n".join(lines)


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")
