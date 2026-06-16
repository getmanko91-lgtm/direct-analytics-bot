from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.services.cpa_style import daily_budget, weekly_budget

BUDGET_TOLERANCE_PERCENT = 10.0


@dataclass(frozen=True)
class BudgetPacing:
    monthly_budget: float
    weekly_budget: float
    daily_budget: float
    period_days: int
    expected_spend: float
    actual_spend: float
    deviation_percent: float | None
    status: str  # ok | over | under | none

    @property
    def has_budget(self) -> bool:
        return self.monthly_budget > 0


def period_day_count(date_from: date, date_to: date) -> int:
    return (date_to - date_from).days + 1


def expected_period_spend(monthly_budget: float, date_from: date, date_to: date) -> float:
    if monthly_budget <= 0:
        return 0.0
    return daily_budget(monthly_budget) * period_day_count(date_from, date_to)


def budget_deviation_percent(actual_spend: float, expected_spend: float) -> float | None:
    if expected_spend <= 0:
        return None
    return ((actual_spend - expected_spend) / expected_spend) * 100.0


def budget_pace_status(deviation_percent: float | None) -> str:
    if deviation_percent is None:
        return "none"
    if abs(deviation_percent) <= BUDGET_TOLERANCE_PERCENT:
        return "ok"
    if deviation_percent > 0:
        return "over"
    return "under"


def build_budget_pacing(
    monthly_budget: float,
    actual_spend: float,
    date_from: date,
    date_to: date,
) -> BudgetPacing:
    month = float(monthly_budget or 0)
    days = period_day_count(date_from, date_to)
    expected = expected_period_spend(month, date_from, date_to)
    deviation = budget_deviation_percent(actual_spend, expected)
    return BudgetPacing(
        monthly_budget=month,
        weekly_budget=weekly_budget(month),
        daily_budget=daily_budget(month),
        period_days=days,
        expected_spend=expected,
        actual_spend=actual_spend,
        deviation_percent=deviation,
        status=budget_pace_status(deviation),
    )
