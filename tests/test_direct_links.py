from datetime import date

from src.services.direct_links import direct_master_report_url, direct_web_master_report_url


def test_master_report_url_opens_direct_report_for_the_period():
    url = direct_master_report_url("porg-test", "search_queries", date(2026, 10, 12), date(2026, 11, 10))
    assert url.startswith("https://direct.yandex.ru/registered/main.pl?")
    assert "ulogin=porg-test" in url
    assert "stat_type=search_queries" in url
    assert "date_from=2026-10-12" in url
    assert "date_to=2026-11-10" in url
    assert "with_nds=1" in url
    assert "cmd=showStat" in url


def test_web_master_report_url_keeps_login_and_period():
    url = direct_web_master_report_url("porg-test", date(2026, 10, 12), date(2026, 11, 10))
    assert url.startswith("https://direct.yandex.ru/dna/reports/wizard?")
    assert "ulogin=porg-test" in url
    assert "date_from=2026-10-12" in url
    assert "date_to=2026-11-10" in url


def test_web_master_report_url_is_empty_without_login():
    assert direct_web_master_report_url("", date(2026, 1, 1), date(2026, 1, 2)) == ""
