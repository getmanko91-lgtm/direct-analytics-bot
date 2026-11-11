from __future__ import annotations

from datetime import date
from urllib.parse import urlencode

# Старые адреса спецотчётов Директ с 22 июня 2026 открывает в новом мастере отчётов.
_REPORT_URL = "https://direct.yandex.ru/registered/main.pl"


def direct_master_report_url(login: str, stat_type: str, date_from: date, date_to: date) -> str:
    """Спецотчёт кабинета за период. Пустой логин — ссылки нет."""
    login = (login or "").strip()
    if not login:
        return ""
    query = {
        "cmd": "showStat",
        "stat_type": stat_type,
        "ulogin": login,
        "show_stat": "1",
        "with_nds": "1",
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    return _REPORT_URL + "?" + urlencode(query)


def direct_web_master_report_url(login: str, date_from: date, date_to: date) -> str:
    """Веб-мастер отчётов Директа по этому кабинету и периоду."""
    login = (login or "").strip()
    if not login:
        return ""
    query = {
        "ulogin": login,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    return "https://direct.yandex.ru/dna/reports/wizard?" + urlencode(query)
