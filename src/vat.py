from __future__ import annotations

VAT_RATE = 0.22


def cost_with_vat(cost: float, vat_rate: float = VAT_RATE) -> float:
    """Применяет НДС к сумме без НДС (IncludeVAT=NO в API)."""
    return cost * (1 + vat_rate)


def cpa_with_vat(cost_without_vat: float, conversions: float, vat_rate: float = VAT_RATE) -> float | None:
    if conversions <= 0:
        return None
    return cost_with_vat(cost_without_vat, vat_rate) / conversions
