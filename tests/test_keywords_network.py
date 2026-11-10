from datetime import date

from src.services.keywords_network import (
    format_keywords_csv,
    format_keywords_telegram,
    keyword_project_misses,
    parse_cabinet_keywords,
)
from src.services.rsya_network import CabinetPlacements, CabinetRef, PlacementStat, build_network_report


def _ref(client_id: int, name: str) -> CabinetRef:
    return CabinetRef(
        client_id=client_id,
        client_name=name,
        directologist="Ксюша",
        yandex_login=f"login-{client_id}",
        goal_ids=(11,),
        attribution_model="AUTO",
    )


def test_parse_keywords_keeps_kpi_campaigns_and_merges_case():
    rows = [
        {
            "CampaignName": "КОНВЕР Поиск",
            "Criterion": "суши доставка",
            "Cost": "1000",
            "Clicks": "5",
            "Impressions": "100",
            "Conversions_11_AUTO": "1",
        },
        {
            "CampaignName": "ЕПК Сеть",
            "Criterion": "Суши  доставка",
            "Cost": "500",
            "Clicks": "3",
            "Impressions": "50",
            "Conversions_11_AUTO": "2",
        },
        {
            "CampaignName": "Охват РСЯ",
            "Criterion": "суши доставка",
            "Cost": "900",
            "Clicks": "9",
            "Impressions": "900",
            "Conversions_11_AUTO": "4",
        },
        {
            "CampaignName": "МК Поиск",
            "Criterion": "---autotargeting",
            "Cost": "800",
            "Clicks": "8",
            "Impressions": "80",
            "Conversions_11_AUTO": "5",
        },
    ]
    parsed = parse_cabinet_keywords(rows, (11,), "AUTO", 0.22)
    assert list(parsed) == ["суши доставка"]
    stat = parsed["суши доставка"]
    assert abs(stat.spend - 1830.0) < 0.01
    assert stat.clicks == 8
    assert stat.conversions == 3


def test_keyword_project_miss_is_one_row_per_project_without_sales():
    cabinets = [
        CabinetPlacements(
            _ref(1, "Япошка"),
            {
                "пицца": PlacementStat(spend=700, clicks=10, impressions=100, conversions=0),
                "роллы": PlacementStat(spend=900, clicks=8, impressions=80, conversions=0),
            },
        ),
        CabinetPlacements(
            _ref(2, "Гринпарк"),
            {
                "пицца": PlacementStat(spend=200, clicks=4, impressions=40, conversions=0),
                "роллы": PlacementStat(spend=800, clicks=6, impressions=60, conversions=3),
            },
        ),
    ]
    report = build_network_report(cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600)
    misses = keyword_project_misses(report, 600)
    assert [(item.project, item.keyword, item.conversions_elsewhere) for item in misses] == [
        ("Япошка", "роллы", 3),
        ("Япошка", "пицца", 0),
    ]


def test_keyword_report_finds_phrase_with_zero_conversions_everywhere():
    cabinets = [
        CabinetPlacements(
            _ref(1, "Япошка"),
            {"пицца": PlacementStat(spend=700, clicks=10, impressions=100, conversions=0)},
        ),
        CabinetPlacements(
            _ref(2, "Гринпарк"),
            {"пицца": PlacementStat(spend=200, clicks=4, impressions=40, conversions=0)},
        ),
    ]
    report = build_network_report(cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600)
    assert [item.placement for item in report.dead] == ["пицца"]
    text = format_keywords_telegram(report)
    assert "Ключевики без конверсий" in text
    assert "пицца — 900 ₽" in text
    csv_text = format_keywords_csv(report)
    assert csv_text.startswith("Ключевик;Расход, ₽")
    assert "пицца;" in csv_text
