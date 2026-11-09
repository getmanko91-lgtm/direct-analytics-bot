from datetime import date
from types import SimpleNamespace

from src.services.client_balances import ClientBalance
from src.services.operator_alerts import (
    CampaignAlert,
    RsyaWasteAlert,
    WeeklyOverspend,
    aggregate_campaigns,
    calendar_week_containing,
    filter_cheap_campaigns,
    filter_dead_campaigns,
    format_cheap_campaigns,
    format_dead_campaigns,
    format_low_balance,
    format_operator_messages,
    format_rsya_waste,
    format_weekly_overspend,
    format_zero_balance,
    is_bidaily_alert_day,
    is_weekly_alert_day,
    last_complete_days,
    split_balance_alerts,
    OperatorAlerts,
    BalanceAlert,
)


def test_monday_sends_weekly_and_week_is_finished_sun_week():
    monday = date(2026, 11, 9)
    sunday = date(2026, 11, 8)
    assert is_weekly_alert_day(monday)
    assert not is_weekly_alert_day(sunday)
    assert calendar_week_containing(sunday) == (date(2026, 11, 2), date(2026, 11, 8))


def test_bidaily_window_is_two_complete_days():
    yesterday = date(2026, 11, 4)
    assert last_complete_days(yesterday, 2) == (date(2026, 11, 3), date(2026, 11, 4))
    first = date(2026, 11, 4)
    second = date(2026, 11, 5)
    assert is_bidaily_alert_day(first) != is_bidaily_alert_day(second)
    assert is_bidaily_alert_day(first) is (first.toordinal() % 2 == 0)


def test_dead_campaign_needs_2000_and_zero_conversions():
    campaigns = [
        ("МК Поиск", 2340.0, 0.0, None),
        ("МК Сеть", 1999.0, 0.0, None),
        ("Бренд", 5000.0, 2.0, 2500.0),
    ]
    dead = filter_dead_campaigns(campaigns)
    assert [item[0] for item in dead] == ["МК Поиск"]


def test_cheap_campaign_is_cpa_below_200():
    campaigns = [
        ("Бренд", 960.0, 8.0, 120.0),
        ("МК", 400.0, 2.0, 200.0),
        ("ЕПК", 150.0, 0.0, None),
    ]
    cheap = filter_cheap_campaigns(campaigns)
    assert [item[0] for item in cheap] == ["Бренд"]


def test_aggregate_campaigns_sums_days_and_applies_vat():
    rows = [
        {
            "CampaignId": "1",
            "CampaignName": "МК Поиск",
            "Cost": "1000",
            "Conversions_11_AUTO": "0",
        },
        {
            "CampaignId": "1",
            "CampaignName": "МК Поиск",
            "Cost": "500",
            "Conversions_11_AUTO": "2",
        },
    ]
    aggregated = aggregate_campaigns(rows, [11], "AUTO", 0.22)
    assert len(aggregated) == 1
    name, spend, conversions, cpa = aggregated[0]
    assert name == "МК Поиск"
    assert abs(spend - 1830.0) < 0.01
    assert conversions == 2
    assert cpa is not None
    assert abs(cpa - 915.0) < 0.01


def test_balance_zero_is_not_also_low():
    clients = [
        SimpleNamespace(name="A", directologist="Ксюша", yandex_login="a"),
        SimpleNamespace(name="B", directologist="Лариса", yandex_login="b"),
        SimpleNamespace(name="C", directologist="Ксюша", yandex_login="c"),
    ]
    balances = {
        "a": ClientBalance(amount=0),
        "b": ClientBalance(amount=7000),
        "c": ClientBalance(amount=7000.01),
    }
    zero, low = split_balance_alerts(clients, balances)
    assert [item.client_name for item in zero] == ["A"]
    assert [item.client_name for item in low] == ["B"]


def test_operator_messages_are_plain_chat_text():
    alerts = OperatorAlerts(
        weekly_overspend=[
            WeeklyOverspend("Япошка г. Сочи", "Ксюша", 14200, 12500, 13.6)
        ],
        dead_campaigns=[
            CampaignAlert("СушиСЕЛЛ г. Курган", "Лариса", "МК Поиск", 2340, 0, None)
        ],
        rsya_waste=[
            RsyaWasteAlert("Япошка г. Сочи", "Ксюша", "example.ru", 890)
        ],
        cheap_campaigns=[
            CampaignAlert("Япошка г. Сочи", "Ксюша", "Бренд", 960, 8, 120)
        ],
        low_balance=[BalanceAlert("Япошка г. Сочи", "Ксюша", 6420)],
        zero_balance=[BalanceAlert("Точка Вкуса г. Сургут", "Лариса", 0)],
        week_from=date(2026, 11, 2),
        week_to=date(2026, 11, 8),
        bidaily_from=date(2026, 11, 3),
        bidaily_to=date(2026, 11, 4),
    )
    messages = format_operator_messages(alerts)
    assert len(messages) == 6
    assert messages[0] == format_weekly_overspend(alerts.weekly_overspend, alerts.week_from, alerts.week_to)
    assert "Операторам" in messages[0]
    assert "Перекрут недельного бюджета" in messages[0]
    assert "<b>" not in messages[0]
    assert "Кампания открутила от 2 000,00 ₽" in format_dead_campaigns(
        alerts.dead_campaigns, alerts.bidaily_from, alerts.bidaily_to
    )
    assert "Площадка РСЯ съела больше 600,00 ₽" in format_rsya_waste(
        alerts.rsya_waste, alerts.week_from, alerts.week_to
    )
    assert "CPA ниже 200,00 ₽" in format_cheap_campaigns(
        alerts.cheap_campaigns, alerts.week_from, alerts.week_to
    )
    assert "7 000,00 ₽ или меньше" in format_low_balance(alerts.low_balance)
    assert "На балансе 0 ₽" in format_zero_balance(alerts.zero_balance)
