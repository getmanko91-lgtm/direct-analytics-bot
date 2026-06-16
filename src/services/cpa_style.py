from __future__ import annotations

CPA_HIGH_THRESHOLD = 500.0
CPA_MEDIUM_THRESHOLD = 300.0
WEEKS_PER_MONTH = 4.0


def weekly_budget(monthly_budget: float) -> float:
    if monthly_budget <= 0:
        return 0.0
    return monthly_budget / WEEKS_PER_MONTH


def cpa_highlight_class(cpa: float | None) -> str:
    if cpa is None:
        return ""
    if cpa > CPA_HIGH_THRESHOLD:
        return "cpa-high"
    if cpa >= CPA_MEDIUM_THRESHOLD:
        return "cpa-medium"
    return ""
