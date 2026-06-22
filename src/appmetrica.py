from __future__ import annotations

import logging
import re
from datetime import date, datetime

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

STAT_DATA_URL = "https://api.appmetrica.yandex.com/stat/v1/data"
EVENTS_URL = "https://api.appmetrica.yandex.com/v1/traffic/sources/events"

BUILTIN_INSTALL_KEY = "__builtin_install__"
BUILTIN_PURCHASE_KEY = "__builtin_purchase__"
BUILTIN_INSTALL_LABEL = "Установки (трекинг AppMetrica)"
BUILTIN_PURCHASE_LABEL = "Покупки In-App Revenue"

BUILTIN_GOALS = (
    (BUILTIN_INSTALL_KEY, BUILTIN_INSTALL_LABEL),
    (BUILTIN_PURCHASE_KEY, BUILTIN_PURCHASE_LABEL),
)


class AppMetricaError(RuntimeError):
    pass


class AppMetricaClient:
    def __init__(self, token: str) -> None:
        self._token = (token or "").strip()
        if not self._token:
            raise AppMetricaError(
                "Не задан токен AppMetrica. Задайте OAuth-токен в Настройках сервиса "
                "(scope appmetrica:read) или в .env (YANDEX_APPMETRICA_TOKEN)."
            )

    def fetch_events(self, application_id: int) -> list[str]:
        try:
            response = requests.get(
                EVENTS_URL,
                params={"appId": application_id},
                headers=self._headers(),
                timeout=60,
            )
        except RequestException as exc:
            raise AppMetricaError("Не удалось подключиться к API AppMetrica") from exc

        if response.status_code == 401:
            raise AppMetricaError("Недействительный токен для API AppMetrica.")
        if response.status_code == 403:
            raise AppMetricaError(f"Нет доступа к приложению AppMetrica {application_id}.")
        if response.status_code == 404:
            raise AppMetricaError(f"Приложение AppMetrica {application_id} не найдено.")
        if response.status_code >= 400:
            raise AppMetricaError(f"AppMetrica API: HTTP {response.status_code}")

        payload = response.json()
        events_info = payload.get("events_info") or {}
        events = events_info.get("events") or []
        return sorted({str(name).strip() for name in events if str(name).strip()})

    def fetch_daily_counts(
        self,
        application_id: int,
        event_key: str,
        date_from: date,
        date_to: date,
        *,
        tracking_id: str | None = None,
    ) -> dict[date, float]:
        tracker_filter = _tracker_filter(tracking_id)
        if event_key == BUILTIN_INSTALL_KEY:
            metrics = ("ym:ts:advInstallDevices",) if tracker_filter else (
                "ym:i:installDevices",
                "ym:ts:advInstallDevices",
            )
            return self._fetch_with_metric_fallbacks(
                application_id,
                metrics,
                date_from,
                date_to,
                extra_filters=tracker_filter,
            )

        if event_key == BUILTIN_PURCHASE_KEY:
            return self._fetch_with_metric_fallbacks(
                application_id,
                (
                    "ym:r:purchaseEvents",
                    "ym:r:inappPurchaseEvents",
                    "ym:r:revenueEvents",
                ),
                date_from,
                date_to,
                extra_filters=tracker_filter,
            )

        metric, event_filter = _metric_spec(event_key)
        combined = _combine_filters(event_filter, tracker_filter)
        return self._fetch_daily_counts_metric(
            application_id, metric, combined, date_from, date_to
        )

    def _fetch_with_metric_fallbacks(
        self,
        application_id: int,
        metrics: tuple[str, ...],
        date_from: date,
        date_to: date,
        *,
        extra_filters: str | None = None,
    ) -> dict[date, float]:
        last_error: AppMetricaError | None = None
        for metric in metrics:
            try:
                return self._fetch_daily_counts_metric(
                    application_id, metric, extra_filters, date_from, date_to
                )
            except AppMetricaError as exc:
                last_error = exc
                logger.warning("AppMetrica metric %s failed: %s", metric, exc)
        if last_error:
            raise last_error
        return {}

    def _fetch_daily_counts_metric(
        self,
        application_id: int,
        metric: str,
        filters: str | None,
        date_from: date,
        date_to: date,
    ) -> dict[date, float]:
        params = {
            "ids": application_id,
            "metrics": metric,
            "dimensions": _date_dimension_for_metric(metric),
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "limit": 10000,
            "accuracy": "full",
        }
        if filters:
            params["filters"] = filters

        payload = self._get_stat_data(params)
        result: dict[date, float] = {}
        for row in payload.get("data") or []:
            dimensions = row.get("dimensions") or []
            metrics = row.get("metrics") or []
            if not dimensions or not metrics:
                continue
            day = _parse_dimension_date(dimensions[0])
            if day is None:
                continue
            result[day] = result.get(day, 0.0) + float(metrics[0] or 0)
        return result

    def _get_stat_data(self, params: dict) -> dict:
        try:
            response = requests.get(
                STAT_DATA_URL,
                params=params,
                headers=self._headers(),
                timeout=120,
            )
        except RequestException as exc:
            raise AppMetricaError("Не удалось подключиться к Reporting API AppMetrica") from exc

        if response.status_code == 401:
            raise AppMetricaError("Недействительный токен для API AppMetrica.")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("message", "")
            except Exception:
                detail = response.text[:200]
            raise AppMetricaError(
                f"AppMetrica Reporting API: HTTP {response.status_code}"
                + (f" — {detail}" if detail else "")
            )

        return response.json()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self._token}"}


def _metric_spec(event_key: str) -> tuple[str, str | None]:
    escaped = event_key.replace("\\", "\\\\").replace("'", "\\'")
    return "ym:ce:eventCount", f"ym:ce:eventLabel=='{escaped}'"


def _date_dimension_for_metric(metric: str) -> str:
    if metric.startswith("ym:ce:"):
        return "ym:ce:date"
    if metric.startswith("ym:i:"):
        return "ym:i:date"
    if metric.startswith("ym:r:"):
        return "ym:r:date"
    if metric.startswith("ym:ts:"):
        return "ym:ts:date"
    return "ym:ge:date"


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _tracker_filter(tracking_id: str | None) -> str | None:
    raw = (tracking_id or "").strip()
    if not raw:
        return None
    escaped = _escape_filter_value(raw)
    if raw.isdigit():
        return f"ym:ts:trackingId=='{escaped}'"
    return f"ym:ts:trackerName=='{escaped}'"


def _combine_filters(*parts: str | None) -> str | None:
    items = [part for part in parts if part]
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    return " AND ".join(items)


def _parse_dimension_date(dimension: dict | str) -> date | None:
    if isinstance(dimension, dict):
        for key in ("iso_date", "name", "id"):
            value = dimension.get(key)
            if value:
                parsed = _parse_date_value(str(value))
                if parsed:
                    return parsed
        return None
    return _parse_date_value(str(dimension))


def _parse_date_value(value: str) -> date | None:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    return None
