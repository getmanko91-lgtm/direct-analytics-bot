from datetime import date

from src.services.rsya_network import (
    CabinetPlacements,
    CabinetRef,
    PlacementStat,
    build_network_report,
    format_network_telegram,
    parse_cabinet_placements,
)


def _ref(client_id: int, name: str) -> CabinetRef:
    return CabinetRef(
        client_id=client_id,
        client_name=name,
        directologist="Ксюша",
        yandex_login=f"login-{client_id}",
        goal_ids=(11,),
        attribution_model="AUTO",
    )


def _stat(spend: float, conversions: float = 0, clicks: int = 10) -> PlacementStat:
    return PlacementStat(spend=spend, clicks=clicks, impressions=clicks * 50, conversions=conversions)


def test_parse_sums_rows_applies_vat_and_counts_selected_goals():
    rows = [
        {"Placement": "game.ru", "Cost": "1000", "Clicks": "5", "Impressions": "100", "Conversions_11_AUTO": "0"},
        {"Placement": "game.ru", "Cost": "500", "Clicks": "3", "Impressions": "50", "Conversions_11_AUTO": "1"},
        {"Placement": "", "Cost": "999", "Clicks": "1", "Impressions": "1"},
    ]
    parsed = parse_cabinet_placements(rows, (11,), "AUTO", 0.22)
    assert list(parsed) == ["game.ru"]
    stat = parsed["game.ru"]
    assert abs(stat.spend - 1830.0) < 0.01
    assert stat.clicks == 8
    assert stat.conversions == 1


def test_dead_placement_is_zero_conversions_across_all_cabinets():
    cabinets = [
        CabinetPlacements(_ref(1, "Япошка"), {"dead.ru": _stat(500), "good.ru": _stat(900, conversions=2)}),
        CabinetPlacements(_ref(2, "Гринпарк"), {"dead.ru": _stat(300), "good.ru": _stat(700)}),
    ]
    report = build_network_report(cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600)

    assert [item.placement for item in report.dead] == ["dead.ru"]
    assert abs(report.dead[0].spend - 800) < 0.01
    assert [share.client_name for share in report.dead[0].cabinets] == ["Япошка", "Гринпарк"]

    assert len(report.partial) == 1
    assert report.partial[0].placement == "good.ru"
    assert report.partial[0].cabinet.client_name == "Гринпарк"
    assert report.partial[0].network_conversions == 2
    assert abs(report.partial[0].network_cpa - 800) < 0.01

    assert abs(report.total_rsya_spend - 2400) < 0.01
    assert abs(report.total_dead_spend - 800) < 0.01
    by_name = {row.client_name: row for row in report.cabinets}
    assert abs(by_name["Япошка"].wasted_spend - 500) < 0.01
    assert by_name["Гринпарк"].wasted_count == 1


def test_good_placements_need_min_conversions_and_cpa_not_above_average():
    cabinets = [
        CabinetPlacements(
            _ref(1, "Япошка"),
            {
                "star.ru": _stat(600, conversions=4, clicks=60),
                "pricey.ru": _stat(2000, conversions=4),
                "rare.ru": _stat(100, conversions=1),
            },
        ),
        CabinetPlacements(_ref(2, "Гринпарк"), {"star.ru": _stat(300, conversions=2, clicks=30)}),
    ]
    report = build_network_report(
        cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600, good_min_conversions=3
    )
    assert abs(report.avg_cpa - 3000 / 11) < 0.01
    assert [item.placement for item in report.good] == ["star.ru"]
    star = report.good[0]
    assert star.conversions == 6
    assert abs(star.cpa - 150) < 0.01
    assert abs(star.cpc - 10) < 0.01
    assert [share.client_name for share in star.cabinets] == ["Япошка", "Гринпарк"]
    assert abs(star.cabinets[1].cpa - 150) < 0.01


def test_threshold_and_errors():
    cabinets = [
        CabinetPlacements(_ref(1, "Япошка"), {"small.ru": _stat(599)}),
        CabinetPlacements(_ref(2, "Сургут"), {}, error="Нет доступа"),
    ]
    report = build_network_report(cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600)
    assert report.dead == ()
    assert report.errors == (("Сургут", "Нет доступа"),)
    assert report.cabinets_total == 2
    assert len(report.cabinets) == 1


def test_telegram_text_lists_top_placements():
    cabinets = [
        CabinetPlacements(_ref(1, "Япошка"), {"dead.ru": _stat(1500)}),
    ]
    report = build_network_report(cabinets, date(2026, 10, 1), date(2026, 10, 31), min_spend=600)
    text = format_network_telegram(report)
    assert "Операторам" in text
    assert "dead.ru — 1 500 ₽, 10 кл., CPC 150,00 ₽, 0 конв. (Япошка)" in text
    assert "Слито: 1 500 ₽ (100% расхода РСЯ)" in text
    assert "Площадок: 1" in text
