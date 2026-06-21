from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

CPA_HIGH_THRESHOLD = 500.0
CPA_MEDIUM_THRESHOLD = 300.0
WEEKS_PER_MONTH = 4.0
DAYS_PER_MONTH = 30.0


def weekly_budget(monthly_budget: float) -> float:
    if monthly_budget <= 0:
        return 0.0
    return monthly_budget / WEEKS_PER_MONTH


def daily_budget(monthly_budget: float) -> float:
    if monthly_budget <= 0:
        return 0.0
    return monthly_budget / DAYS_PER_MONTH


def cpa_highlight_class(cpa: float | None) -> str:
    if cpa is None or cpa <= 0:
        return ""
    if cpa > CPA_HIGH_THRESHOLD:
        return "cpa-high"
    if cpa >= CPA_MEDIUM_THRESHOLD:
        return "cpa-medium"
    return "cpa-low"


def cpa_chart_color(cpa: float | None) -> str:
    css_class = cpa_highlight_class(cpa)
    return {
        "cpa-high": "rgba(252, 63, 29, 0.85)",
        "cpa-medium": "rgba(255, 193, 7, 0.85)",
        "cpa-low": "rgba(61, 214, 140, 0.85)",
    }.get(css_class, "rgba(110, 182, 255, 0.75)")


def previous_period(date_from: date, date_to: date) -> tuple[date, date]:
    """Предыдущий период той же длины, сразу до выбранного."""
    days = (date_to - date_from).days + 1
    prev_to = date_from - timedelta(days=1)
    prev_from = prev_to - timedelta(days=days - 1)
    return prev_from, prev_to


def format_period_short(date_from: date, date_to: date) -> str:
    if date_from == date_to:
        return date_from.strftime("%d.%m.%Y")
    return f"{date_from.strftime('%d.%m.%Y')} — {date_to.strftime('%d.%m.%Y')}"


def cpa_compare_display(
    current: float | None,
    previous: float | None,
    *,
    fmt_money: Callable[[float], str],
) -> tuple[str, str, str]:
    """Короткий текст сравнения CPA, CSS-класс и подсказка (ниже CPA — лучше)."""
    if current is None or previous is None or previous <= 0:
        return "", "", ""

    pct = (current - previous) / previous * 100
    prev_text = fmt_money(previous)
    title = f"Было {prev_text} ₽"

    if abs(pct) < 0.5:
        return "=", "cpa-compare-neutral", title

    if pct < 0:
        return f"▼{abs(pct):.0f}%", "cpa-compare-good", title

    return f"▲+{pct:.0f}%", "cpa-compare-bad", title

