from __future__ import annotations

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
