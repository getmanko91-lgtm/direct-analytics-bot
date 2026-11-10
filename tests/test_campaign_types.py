from datetime import date

from src.services.campaign_types import (
    CabinetCampaigns,
    CampaignFact,
    best_labels,
    build_campaign_types_report,
    campaign_type_of,
    describe_strategy,
    parse_cabinet_facts,
    strategies_by_campaign,
)
from src.services.rsya_network import CabinetRef


def _ref(client_id: int, name: str) -> CabinetRef:
    return CabinetRef(
        client_id=client_id,
        client_name=name,
        directologist="Ксюша",
        yandex_login=f"login-{client_id}",
        goal_ids=(11,),
        attribution_model="AUTO",
    )


def test_campaign_type_uses_first_word_and_known_aliases():
    assert campaign_type_of("ТМК Поиск Сочи") == "ТМК"
    assert campaign_type_of("мк рся") == "МК"
    assert campaign_type_of("КОНВЕРСИИ бренд") == "КОНВЕР"
    assert campaign_type_of("КОНВЕРСИ поиск") == "КОНВЕР"
    assert campaign_type_of("ЕПК-доставка") == "ЕПК"
    assert campaign_type_of("123 акция") == "ДРУГИЕ"


def test_describe_strategy_payment_model_and_budget():
    weekly = {
        "Search": {
            "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
            "WbMaximumConversionRate": {"WeeklySpendLimit": 5_000_000_000, "BudgetType": "WEEKLY_BUDGET"},
        },
        "Network": {"BiddingStrategyType": "SERVING_OFF"},
    }
    assert describe_strategy(weekly) == ("Максимум конверсий", "За клики", "Недельный бюджет")

    pay = {
        "Search": {
            "BiddingStrategyType": "PAY_FOR_CONVERSION",
            "PayForConversion": {"BudgetType": "CUSTOM_PERIOD_BUDGET", "Cpa": 300_000_000},
        },
        "Network": {
            "BiddingStrategyType": "PAY_FOR_CONVERSION",
            "PayForConversion": {"BudgetType": "CUSTOM_PERIOD_BUDGET"},
        },
    }
    assert describe_strategy(pay) == ("Оплата за конверсии", "За конверсии", "Бюджет на период")

    mixed = {
        "Search": {"BiddingStrategyType": "WB_MAXIMUM_CLICKS", "WbMaximumClicks": {"WeeklySpendLimit": 1}},
        "Network": {"BiddingStrategyType": "PAY_FOR_CONVERSION", "PayForConversion": {"BudgetType": "WEEKLY_BUDGET"}},
    }
    assert describe_strategy(mixed) == (
        "Поиск: Максимум кликов · РСЯ: Оплата за конверсии",
        "Смешанная",
        "Недельный бюджет",
    )


def test_strategy_index_accepts_numeric_campaign_id():
    campaigns = [
        {
            "Id": 15,
            "TextCampaign": {
                "BiddingStrategy": {
                    "Search": {
                        "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
                        "WbMaximumClicks": {"WeeklySpendLimit": 1_000_000},
                    },
                    "Network": {"BiddingStrategyType": "SERVING_OFF"},
                }
            },
        }
    ]
    assert strategies_by_campaign(campaigns)[15][0] == "Максимум кликов"


def test_types_are_summed_across_cabinets_and_ranked_by_sales():
    rows = [
        {"CampaignId": "1", "CampaignName": "МК Поиск", "Cost": "1000", "Clicks": "10", "Impressions": "100", "Conversions_11_AUTO": "2"},
        {"CampaignId": "1", "CampaignName": "МК Поиск", "Cost": "500", "Clicks": "5", "Impressions": "40", "Conversions_11_AUTO": "1"},
        {"CampaignId": "2", "CampaignName": "ЕПК Сеть", "Cost": "2000", "Clicks": "4", "Impressions": "80", "Conversions_11_AUTO": "1"},
    ]
    strategies = {
        1: ("Максимум конверсий", "За клики", "Недельный бюджет"),
        2: ("Оплата за конверсии", "За конверсии", "Бюджет на период"),
    }
    facts = parse_cabinet_facts(rows, strategies, _ref(1, "Япошка"), 0.22)
    other = CampaignFact(
        client_id=2,
        client_name="Гринпарк",
        directologist="Лариса",
        campaign_type="МК",
        strategy="Максимум конверсий",
        payment="За клики",
        budget="Недельный бюджет",
        spend=1220,
        clicks=8,
        impressions=50,
        conversions=4,
    )
    report = build_campaign_types_report(
        [
            CabinetCampaigns(cabinet=_ref(1, "Япошка"), facts=facts),
            CabinetCampaigns(cabinet=_ref(2, "Гринпарк"), facts=[other]),
        ],
        date(2026, 10, 1),
        date(2026, 10, 31),
    )
    assert [item.label for item in report.by_type] == ["МК", "ЕПК"]
    mk = report.by_type[0]
    assert mk.conversions == 7
    assert mk.campaigns == 2
    assert [cabinet.client_name for cabinet in mk.cabinets] == ["Гринпарк", "Япошка"]
    assert abs(mk.cabinets[1].spend - 1830) < 0.01
    assert [item.label for item in report.by_payment] == ["За клики", "За конверсии"]
    assert best_labels(report.by_type) == ("МК", "МК")
