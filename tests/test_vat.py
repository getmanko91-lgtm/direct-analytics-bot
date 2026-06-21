from src.vat import cost_with_vat, cpa_with_vat


def test_cpa_equals_spend_with_vat_divided_by_conversions():
    spend_raw = 4146.01639344
    conversions = 13
    vat = 0.22
    spend = cost_with_vat(spend_raw, vat)
    cpa = cpa_with_vat(spend_raw, conversions, vat)
    assert abs(spend - 5058.14) < 0.02
    assert abs(cpa - spend / conversions) < 0.01
    assert abs(cpa - 389.09) < 0.05
