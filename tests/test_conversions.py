from src.yandex_direct import conversions_for_goal


def test_conversions_use_configured_model_not_fallback():
    row = {
        "Conversions_123_AUTO": "120",
        "Conversions_123_LYDC": "86",
    }
    assert conversions_for_goal(row, 123, "AUTO") == 120
    assert conversions_for_goal(row, 123, "LYDC") == 86


def test_conversions_zero_in_primary_without_fallback():
    row = {
        "Conversions_123_AUTO": "0",
        "Conversions_123_LYDC": "86",
    }
    assert conversions_for_goal(row, 123, "AUTO") == 0


def test_conversions_legacy_column_without_model_suffix():
    row = {"Conversions_123": "120"}
    assert conversions_for_goal(row, 123, "AUTO") == 120
