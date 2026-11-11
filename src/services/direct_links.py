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


# Модели из карточки клиента → значения, которые читает мастер отчётов.
_WIZARD_ATTRIBUTION = {
    "AUTO": "AUTOMATIC",
    "LSC": "LAST_SIGNIFICANT_CLICK",
    "LYDC": "LAST_YANDEX_DIRECT_CLICK",
    "LC": "LAST_CLICK",
    "FC": "FIRST_CLICK",
    "FCCD": "FIRST_CLICK_CROSS_DEVICE",
    "LSCCD": "LAST_SIGNIFICANT_CLICK_CROSS_DEVICE",
    "LYDCCD": "LAST_YANDEX_DIRECT_CLICK_CROSS_DEVICE",
}


def direct_web_master_report_url(
    login: str,
    date_from: date,
    date_to: date,
    goal_ids: list[int] | None = None,
    attribution_model: str | None = None,
) -> str:
    """Веб-мастер отчётов Директа: тот же период и те же цели, что в учёте конверсий.

    Мастер читает период как from/to и выбранные цели Метрики как goalIds
    через запятую. date_from/date_to оставляем: по ним период тоже открывался.
    """
    login = (login or "").strip()
    if not login:
        return ""
    query = {
        "ulogin": login,
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
    }
    selected = []
    for goal_id in goal_ids or []:
        if goal_id and goal_id not in selected:
            selected.append(goal_id)
    if selected:
        query["goalIds"] = ",".join(str(goal_id) for goal_id in selected)
    attribution = _WIZARD_ATTRIBUTION.get((attribution_model or "").strip().upper())
    if attribution:
        query["attributionModel"] = attribution
    return "https://direct.yandex.ru/dna/reports/wizard?" + urlencode(query)
