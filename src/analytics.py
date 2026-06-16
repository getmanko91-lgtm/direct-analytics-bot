from __future__ import annotations

from dataclasses import dataclass

from src.yandex_direct import CampaignStats, DailyStats


@dataclass(frozen=True)
class MetricDelta:
    current: float
    previous: float

    @property
    def absolute(self) -> float:
        return self.current - self.previous

    @property
    def percent(self) -> float | None:
        if self.previous == 0:
            return None if self.current == 0 else 100.0
        return (self.absolute / self.previous) * 100


def format_report(
    yesterday: DailyStats,
    day_before: DailyStats | None,
    spend_alert_threshold: float = 0,
    client_name: str | None = None,
    goal_names: list[str] | None = None,
    vat_percent: int = 22,
) -> str:
    title = client_name or "Яндекс.Директ"
    lines = [
        f"📊 <b>{_escape_html(title)} — {yesterday.report_date.strftime('%d.%m.%Y')}</b>",
    ]
    if goal_names:
        goals_text = ", ".join(_escape_html(name) for name in goal_names)
        lines.append(f"🎯 Цели: {goals_text}")
    lines.append(f"💰 Суммы с НДС {vat_percent}%")
    lines.append("")
    lines.extend([_format_totals(yesterday, day_before)])

    if yesterday.campaigns:
        lines.extend(["", "<b>По кампаниям:</b>"])
        for campaign in sorted(yesterday.campaigns, key=lambda c: c.cost, reverse=True):
            lines.append(_format_campaign_line(campaign))

    alerts = _build_alerts(yesterday, day_before, spend_alert_threshold)
    if alerts:
        lines.extend(["", "<b>⚠️ Сигналы:</b>", *alerts])

    return "\n".join(lines)


def _format_totals(current: DailyStats, previous: DailyStats | None) -> str:
    rows = [
        ("Показы", current.impressions, previous.impressions if previous else None, _format_int),
        ("Клики", current.clicks, previous.clicks if previous else None, _format_int),
        ("Расход", current.cost, previous.cost if previous else None, _format_money),
        ("CTR", current.ctr, previous.ctr if previous else None, _format_percent),
        ("CPC", current.avg_cpc, previous.avg_cpc if previous else None, _format_money),
        ("Конверсии", current.conversions, previous.conversions if previous else None, _format_float),
    ]

    cpa_current = current.cost_per_conversion
    cpa_previous = previous.cost_per_conversion if previous else None
    rows.append(("CPA", cpa_current, cpa_previous, _format_money_optional))

    lines = ["<b>Итого за день:</b>"]
    for label, cur, prev, formatter in rows:
        lines.append(_format_metric_line(label, cur, prev, formatter))
    return "\n".join(lines)


def _format_metric_line(
    label: str,
    current: float | None,
    previous: float | None,
    formatter,
) -> str:
    if current is None:
        return f"• {label}: —"

    current_text = formatter(current)
    if previous is None:
        return f"• {label}: {current_text}"

    delta = MetricDelta(float(current), float(previous))
    return f"• {label}: {current_text} ({_format_delta(delta)})"


def _format_campaign_line(campaign: CampaignStats) -> str:
    cpa = campaign.cost_per_conversion
    cpa_text = _format_money(cpa) if campaign.conversions > 0 else "—"
    return (
        f"• <i>{_escape_html(campaign.campaign_name)}</i>: "
        f"{_format_money(campaign.cost)}, "
        f"{_format_int(campaign.clicks)} кл., "
        f"CTR {_format_percent(campaign.ctr)}, "
        f"CPA {cpa_text}"
    )


def _build_alerts(
    yesterday: DailyStats,
    day_before: DailyStats | None,
    spend_alert_threshold: float,
) -> list[str]:
    alerts: list[str] = []

    if spend_alert_threshold > 0 and yesterday.cost >= spend_alert_threshold:
        alerts.append(
            f"Расход { _format_money(yesterday.cost) } превысил порог "
            f"{_format_money(spend_alert_threshold)}"
        )

    if day_before:
        spend_delta = MetricDelta(yesterday.cost, day_before.cost)
        if spend_delta.percent is not None and spend_delta.percent >= 30:
            alerts.append(
                f"Расход вырос на {_format_percent(spend_delta.percent)} "
                f"относительно {_format_short_date(day_before.report_date)}"
            )
        elif spend_delta.percent is not None and spend_delta.percent <= -30:
            alerts.append(
                f"Расход снизился на {_format_percent(abs(spend_delta.percent))} "
                f"относительно {_format_short_date(day_before.report_date)}"
            )

        clicks_delta = MetricDelta(yesterday.clicks, day_before.clicks)
        if clicks_delta.percent is not None and clicks_delta.percent <= -40 and yesterday.clicks > 0:
            alerts.append(
                f"Клики упали на {_format_percent(abs(clicks_delta.percent))} "
                f"относительно {_format_short_date(day_before.report_date)}"
            )

        for campaign in yesterday.campaigns:
            prev = _find_campaign(day_before, campaign.campaign_name)
            if prev and prev.cost > 0 and campaign.cost == 0:
                alerts.append(f"Кампания «{_escape_html(campaign.campaign_name)}» потратила 0 ₽")

    if yesterday.impressions == 0 and yesterday.clicks == 0 and yesterday.cost == 0:
        alerts.append("За вчера нет активности — проверьте кампании")

    return alerts


def _find_campaign(stats: DailyStats, campaign_name: str) -> CampaignStats | None:
    for campaign in stats.campaigns:
        if campaign.campaign_name == campaign_name:
            return campaign
    return None


def _format_delta(delta: MetricDelta) -> str:
    sign = "+" if delta.absolute >= 0 else "−"
    abs_value = abs(delta.absolute)
    if delta.percent is None:
        return f"{sign}{_format_number(abs_value)} vs пред. день"
    percent_sign = "+" if delta.percent >= 0 else "−"
    return f"{sign}{_format_number(abs_value)} ({percent_sign}{abs(delta.percent):.1f}%)"


def _format_int(value: float) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def _format_float(value: float) -> str:
    if float(value).is_integer():
        return _format_int(value)
    return f"{value:.2f}".replace(".", ",")


def _format_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _format_money_optional(value: float | None) -> str:
    if value is None:
        return "—"
    return _format_money(value)


def _format_percent(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + "%"


def _format_short_date(value) -> str:
    return value.strftime("%d.%m")


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return _format_int(value)
    return f"{value:.2f}".replace(".", ",")


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
