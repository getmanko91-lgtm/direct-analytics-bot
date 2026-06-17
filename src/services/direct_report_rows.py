from __future__ import annotations

from datetime import date

from src.config import Settings
from src.db.models import Client
from src.services.runtime_cache import get_or_set
from src.yandex_direct import (
    BASE_REPORT_FIELDS,
    MAX_GOALS_PER_REQUEST,
    YandexDirectClient,
    _chunked,
    _merge_conversion_columns,
    _report_row_key,
)

CACHE_TTL_SECONDS = 120

STANDARD_FIELD_NAMES = [
    *BASE_REPORT_FIELDS,
    "Conversions",
    "CostPerConversion",
]

CLIENT_REPORT_FIELD_NAMES = [
    *BASE_REPORT_FIELDS,
    "Conversions",
    "Revenue",
]


def _merge_goal_chunks(
    api: YandexDirectClient,
    date_from: date,
    date_to: date,
    goal_ids: list[int],
    attribution_model: str,
    field_names: list[str],
    *,
    report_name_prefix: str,
) -> list[dict[str, str]]:
    def fetch_chunk(chunk: list[int]) -> list[dict[str, str]]:
        return api._fetch_report_ex(
            date_from,
            date_to,
            chunk,
            attribution_model,
            report_type="CAMPAIGN_PERFORMANCE_REPORT",
            field_names=field_names,
            report_name_prefix=report_name_prefix,
        )

    if not goal_ids:
        return fetch_chunk([])

    merged: dict[tuple[str, str], dict[str, str]] = {}
    for chunk in _chunked(goal_ids, MAX_GOALS_PER_REQUEST):
        for row in fetch_chunk(list(chunk)):
            key = _report_row_key(row)
            if key not in merged:
                merged[key] = dict(row)
            else:
                _merge_conversion_columns(merged[key], row, list(chunk))
    return list(merged.values())


def fetch_campaign_performance_rows(
    api: YandexDirectClient,
    date_from: date,
    date_to: date,
    goal_ids: list[int],
    attribution_model: str,
) -> list[dict[str, str]]:
    return _merge_goal_chunks(
        api,
        date_from,
        date_to,
        goal_ids,
        attribution_model,
        STANDARD_FIELD_NAMES,
        report_name_prefix="PerfRows",
    )


def fetch_client_report_rows(
    api: YandexDirectClient,
    date_from: date,
    date_to: date,
    goal_ids: list[int],
    attribution_model: str,
) -> list[dict[str, str]]:
    try:
        return _merge_goal_chunks(
            api,
            date_from,
            date_to,
            goal_ids,
            attribution_model,
            CLIENT_REPORT_FIELD_NAMES,
            report_name_prefix="ClientReport",
        )
    except Exception:
        fields_without_revenue = [name for name in CLIENT_REPORT_FIELD_NAMES if name != "Revenue"]
        return _merge_goal_chunks(
            api,
            date_from,
            date_to,
            goal_ids,
            attribution_model,
            fields_without_revenue,
            report_name_prefix="ClientReport",
        )


def fetch_campaign_performance_rows_cached(
    settings: Settings,
    client: Client,
    date_from: date,
    date_to: date,
) -> list[dict[str, str]]:
    goal_ids = tuple(sorted(g.goal_id for g in client.goals if g.is_selected))
    key = (
        "perf_rows",
        settings.yandex_token,
        client.id,
        date_from.isoformat(),
        date_to.isoformat(),
        client.attribution_model,
        goal_ids,
        "standard",
    )

    def _load() -> list[dict[str, str]]:
        api = YandexDirectClient(settings.yandex_token, client.yandex_login)
        return fetch_campaign_performance_rows(
            api,
            date_from,
            date_to,
            list(goal_ids),
            client.attribution_model,
        )

    return get_or_set(key, _load, ttl_seconds=CACHE_TTL_SECONDS)


def fetch_client_report_rows_cached(
    settings: Settings,
    client: Client,
    date_from: date,
    date_to: date,
) -> list[dict[str, str]]:
    goal_ids = tuple(sorted(g.goal_id for g in client.goals if g.is_selected))
    key = (
        "perf_rows",
        settings.yandex_token,
        client.id,
        date_from.isoformat(),
        date_to.isoformat(),
        client.attribution_model,
        goal_ids,
        "client_report",
    )

    def _load() -> list[dict[str, str]]:
        api = YandexDirectClient(settings.yandex_token, client.yandex_login)
        return fetch_client_report_rows(
            api,
            date_from,
            date_to,
            list(goal_ids),
            client.attribution_model,
        )

    return get_or_set(key, _load, ttl_seconds=CACHE_TTL_SECONDS)
