from src.services.ad_texts import (
    MISSING_TEXT,
    AdStat,
    CabinetAds,
    apply_ad_copies,
    best_text_labels,
    build_ad_texts_report,
    extract_ad_copy,
    parse_cabinet_ads,
    text_identity,
)
from src.services.rsya_network import CabinetRef
from datetime import date


def _ref(client_id: int, name: str) -> CabinetRef:
    return CabinetRef(
        client_id=client_id,
        client_name=name,
        directologist="Ксюша",
        yandex_login=f"login-{client_id}",
        goal_ids=(11,),
        attribution_model="AUTO",
    )


def test_extract_ad_copy_reads_text_and_responsive_ads():
    assert extract_ad_copy(
        {"TextAd": {"Title": "Суши", "Title2": "за 30 минут", "Text": "Доставка по городу"}}
    ) == ("Суши — за 30 минут", "Доставка по городу")
    assert extract_ad_copy(
        {"ResponsiveAd": {"Titles": ["Роллы", "Сеты"], "Texts": [{"Text": "Скидка вечером"}]}}
    ) == ("Роллы | Сеты", "Скидка вечером")
    assert extract_ad_copy({"Id": 5}) == ("", "")
    assert extract_ad_copy({"ShoppingAd": {"DefaultTexts": ["Сет за 999 ₽"]}}) == ("", "Сет за 999 ₽")


def test_same_text_keeps_one_identity_and_missing_text_stays_per_ad():
    assert text_identity("Суши", "Доставка", 1) == text_identity("суши", "  доставка ", 9)
    assert text_identity("", "", 4) != text_identity("", "", 5)


def test_parse_counts_only_kpi_campaigns_and_selected_goals():
    rows = [
        {"CampaignName": "КОНВЕР поиск", "AdId": "7", "Cost": "1000", "Clicks": "10", "Impressions": "100", "Conversions_11_AUTO": "3"},
        {"CampaignName": "ИМИДЖ охват", "AdId": "8", "Cost": "5000", "Clicks": "40", "Impressions": "400", "Conversions_11_AUTO": "9"},
        {"CampaignName": "МК сеть", "AdId": "7", "Cost": "200", "Clicks": "2", "Impressions": "20", "Conversions_11_AUTO": "1"},
    ]
    ads = parse_cabinet_ads(rows, [11], "AUTO", 0.22)
    assert set(ads) == {7}
    assert ads[7].conversions == 4
    assert ads[7].spend == 1200 * 1.22


def test_texts_are_ranked_across_cabinets_by_conversions():
    sushi = AdStat(1, "Суши", "за 30 минут", spend=1000, clicks=20, impressions=200, conversions=5)
    other = AdStat(2, "Пицца", "бесплатная доставка", spend=400, clicks=8, impressions=80, conversions=1)
    same = AdStat(3, "суши", "за 30 минут", spend=500, clicks=10, impressions=90, conversions=4)
    missing = AdStat(4, "", "", spend=50, clicks=1, impressions=10, conversions=0)
    report = build_ad_texts_report(
        [
            CabinetAds(cabinet=_ref(1, "Япошка"), ads=[sushi, other]),
            CabinetAds(cabinet=_ref(2, "Гринпарк"), ads=[same, missing]),
        ],
        date(2026, 10, 11),
        date(2026, 11, 9),
    )
    assert [item.label for item in report.texts] == [
        "Суши — за 30 минут",
        "Пицца — бесплатная доставка",
        MISSING_TEXT,
    ]
    assert report.texts[0].conversions == 9
    assert report.texts[0].spend == 1500
    assert [share.client_name for share in report.texts[0].cabinets] == ["Япошка", "Гринпарк"]
    assert report.texts_with_sales == 2
    best_conversions, best_cpa = best_text_labels(report)
    assert best_conversions is report.texts[0]
    assert best_cpa is report.texts[0]


def test_apply_ad_copies_accepts_numeric_id():
    ads = {4: AdStat(4, "", "", spend=10, clicks=1, impressions=1, conversions=1)}
    apply_ad_copies(ads, [{"Id": 4, "TextAd": {"Title": "Роллы", "Text": "Сет"}}])
    assert ads[4].title == "Роллы"
    assert ads[4].body == "Сет"
