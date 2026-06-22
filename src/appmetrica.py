from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

STAT_DATA_URL = "https://api.appmetrica.yandex.com/stat/v1/data"
EVENTS_URL = "https://api.appmetrica.yandex.com/v1/traffic/sources/events"
TRACKERS_URL = "https://api.appmetrica.yandex.com/management/v1/application/{application_id}/trackers"
_SERVE_HASH_RE = re.compile(r"/serve/(\d+)")

BUILTIN_INSTALL_KEY = "__builtin_install__"
BUILTIN_PURCHASE_KEY = "__builtin_purchase__"
BUILTIN_INSTALL_LABEL = "Установки (трекинг AppMetrica)"
BUILTIN_PURCHASE_LABEL = "Покупки In-App Revenue"

BUILTIN_GOALS = (
    (BUILTIN_INSTALL_KEY, BUILTIN_INSTALL_LABEL),
    (BUILTIN_PURCHASE_KEY, BUILTIN_PURCHASE_LABEL),
)

_TRACKER_DIMENSION = "ym:ts:tracker"


@dataclass(frozen=True)
class ResolvedTracker:
    tracking_id: str
    name: str


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

    def resolve_tracker(self, application_id: int, tracker_ref: str) -> ResolvedTracker:
        ref = tracker_ref.strip()
        if not ref:
            raise AppMetricaError("Не задан трекер AppMetrica.")

        serve_hash = _extract_serve_hash(ref)
        trackers = self._list_trackers(application_id)
        ref_lower = ref.lower()
        for tracker in trackers:
            tracking_id = str(tracker.get("id", "")).strip()
            name = str(tracker.get("name", "")).strip()
            urls = " ".join(
                str(tracker.get(key, ""))
                for key in (
                    "tracking_url",
                    "url",
                    "click_url",
                    "tracking_link",
                    "tracking_urls",
                    "impression_url",
                )
            )
            if not tracking_id:
                continue
            if ref == tracking_id or ref == name or ref_lower == name.lower():
                return ResolvedTracker(tracking_id, name or tracking_id)
            if serve_hash and serve_hash in urls:
                return ResolvedTracker(tracking_id, name or tracking_id)

        if ref.isdigit() and len(ref) <= 12:
            return ResolvedTracker(ref, ref)

        if serve_hash:
            raise AppMetricaError(
                f"Трекер с ссылкой /serve/{serve_hash} не найден в приложении AppMetrica {application_id}. "
                "Проверьте ID приложения и токен AppMetrica."
            )
        raise AppMetricaError(
            f"Трекер «{ref}» не найден в приложении AppMetrica {application_id}. "
            "Укажите числовой ID трекера или полную трекинговую ссылку из AppMetrica → Трекинг."
        )

    def fetch_daily_counts(
        self,
        application_id: int,
        event_key: str,
        date_from: date,
        date_to: date,
        *,
        tracking_id: str | None = None,
    ) -> dict[date, float]:
        tracker_ref = (tracking_id or "").strip() or None
        resolved_tracker = (
            self.resolve_tracker(application_id, tracker_ref) if tracker_ref else None
        )
        if event_key == BUILTIN_INSTALL_KEY:
            metrics = ("ym:ts:advInstallDevices",) if tracker_ref else (
                "ym:i:installDevices",
                "ym:ts:advInstallDevices",
            )
            return self._fetch_with_metric_fallbacks(
                application_id,
                metrics,
                date_from,
                date_to,
                resolved_tracker=resolved_tracker,
            )

        if event_key == BUILTIN_PURCHASE_KEY:
            metrics = (
                "ym:ts:purchaseEvents",
                "ym:ts:inappPurchaseEvents",
                "ym:ts:revenueEvents",
                "ym:r:purchaseEvents",
                "ym:r:inappPurchaseEvents",
                "ym:r:revenueEvents",
            ) if tracker_ref else (
                "ym:r:purchaseEvents",
                "ym:r:inappPurchaseEvents",
                "ym:r:revenueEvents",
            )
            return self._fetch_with_metric_fallbacks(
                application_id,
                metrics,
                date_from,
                date_to,
                resolved_tracker=resolved_tracker,
            )

        metric, event_filter = _metric_spec(event_key)
        return self._fetch_with_metric_fallbacks(
            application_id,
            (metric,),
            date_from,
            date_to,
            extra_filters=None,
            resolved_tracker=resolved_tracker,
            event_filter=event_filter,
        )

    def _fetch_with_metric_fallbacks(
        self,
        application_id: int,
        metrics: tuple[str, ...],
        date_from: date,
        date_to: date,
        *,
        extra_filters: str | None = None,
        resolved_tracker: ResolvedTracker | None = None,
        event_filter: str | None = None,
    ) -> dict[date, float]:
        last_error: AppMetricaError | None = None
        for metric in metrics:
            try:
                if resolved_tracker:
                    return self._fetch_tracked_daily_counts(
                        application_id,
                        metric,
                        resolved_tracker,
                        date_from,
                        date_to,
                        event_filter=event_filter or extra_filters,
                    )
                combined = _combine_filters(event_filter, extra_filters)
                return self._fetch_daily_counts_metric(
                    application_id, metric, combined, date_from, date_to
                )
            except AppMetricaError as exc:
                last_error = exc
                logger.warning("AppMetrica metric %s failed: %s", metric, exc)
        if last_error:
            raise last_error
        return {}

    def _fetch_tracked_daily_counts(
        self,
        application_id: int,
        metric: str,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
        *,
        event_filter: str | None = None,
    ) -> dict[date, float]:
        last_error: AppMetricaError | None = None
        for tracker_filter in _tracker_filter_variants(tracker):
            combined = _combine_filters(event_filter, tracker_filter)
            try:
                return self._fetch_daily_counts_metric(
                    application_id, metric, combined, date_from, date_to
                )
            except AppMetricaError as exc:
                if not _is_tracker_filter_rejected(exc):
                    raise
                last_error = exc
                logger.warning(
                    "Tracker filter %r rejected for %s: %s",
                    tracker_filter,
                    metric,
                    exc,
                )

        if metric.startswith("ym:ts:"):
            try:
                return self._fetch_daily_counts_by_tracker_dimension(
                    application_id,
                    metric,
                    tracker,
                    date_from,
                    date_to,
                    event_filter=event_filter,
                )
            except AppMetricaError as exc:
                last_error = exc

        raise _tracker_filter_error(tracker, last_error)

    def _fetch_daily_counts_by_tracker_dimension(
        self,
        application_id: int,
        metric: str,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
        *,
        event_filter: str | None = None,
    ) -> dict[date, float]:
        if not metric.startswith("ym:ts:"):
            raise AppMetricaError(
                f"Метрика {metric} не поддерживает группировку по трекеру."
            )

        date_dim = _date_dimension_for_metric(metric)
        params = {
            "ids": application_id,
            "metrics": metric,
            "dimensions": f"{date_dim},{_TRACKER_DIMENSION}",
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "limit": 10000,
            "accuracy": "full",
        }
        if event_filter:
            params["filters"] = event_filter

        payload = self._get_stat_data(params)
        result: dict[date, float] = {}
        for row in payload.get("data") or []:
            dimensions = row.get("dimensions") or []
            metrics = row.get("metrics") or []
            if len(dimensions) < 2 or not metrics:
                continue
            if not _tracker_dimension_matches(dimensions[1], tracker):
                continue
            day = _parse_dimension_date(dimensions[0])
            if day is None:
                continue
            result[day] = result.get(day, 0.0) + float(metrics[0] or 0)
        return result

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

    def _list_trackers(self, application_id: int) -> list[dict]:
        url = TRACKERS_URL.format(application_id=application_id)
        try:
            response = requests.get(
                url,
                params={"limit": 1000},
                headers=self._headers(),
                timeout=60,
            )
        except RequestException as exc:
            raise AppMetricaError("Не удалось подключиться к Management API AppMetrica") from exc

        if response.status_code == 401:
            raise AppMetricaError("Недействительный токен для API AppMetrica.")
        if response.status_code == 403:
            raise AppMetricaError(f"Нет доступа к приложению AppMetrica {application_id}.")
        if response.status_code == 404:
            raise AppMetricaError(f"Приложение AppMetrica {application_id} не найдено.")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("message", "")
            except Exception:
                detail = response.text[:200]
            raise AppMetricaError(
                f"AppMetrica Management API: HTTP {response.status_code}"
                + (f" — {detail}" if detail else "")
            )

        payload = response.json()
        trackers = payload.get("trackers")
        if isinstance(trackers, list):
            return trackers
        if isinstance(payload, list):
            return payload
        return []

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


def _extract_serve_hash(tracker_ref: str) -> str:
    match = _SERVE_HASH_RE.search(tracker_ref)
    return match.group(1) if match else ""


def _tracker_filter_variants(tracker: ResolvedTracker) -> tuple[str, ...]:
    variants: list[str] = []
    if tracker.name and tracker.name != tracker.tracking_id:
        variants.append(f"ym:ts:tracker=='{_escape_filter_value(tracker.name)}'")
    variants.append(f"ym:ts:tracker=='{_escape_filter_value(tracker.tracking_id)}'")
    return tuple(dict.fromkeys(variants))


def _is_tracker_filter_rejected(exc: AppMetricaError) -> bool:
    text = str(exc)
    if "HTTP 400" not in text and "Incorrectly specified" not in text:
        return False
    return "tracker" in text.lower() or "filter" in text.lower() or "attribute" in text.lower()


def _tracker_filter_error(
    tracker: ResolvedTracker,
    last_error: AppMetricaError | None,
) -> AppMetricaError:
    label = tracker.name if tracker.name != tracker.tracking_id else tracker.tracking_id
    message = (
        f"Не удалось отфильтровать данные по трекеру «{label}» (ID {tracker.tracking_id}). "
        "Проверьте, что трекер относится к этому приложению AppMetrica."
    )
    if last_error:
        return AppMetricaError(f"{message} {last_error}")
    return AppMetricaError(message)


def _tracker_dimension_matches(dimension: dict | str, tracker: ResolvedTracker) -> bool:
    if not isinstance(dimension, dict):
        value = str(dimension).strip()
        return value in {tracker.tracking_id, tracker.name}
    name = str(dimension.get("name", "")).strip()
    dim_id = str(dimension.get("id", "")).strip()
    if tracker.tracking_id and tracker.tracking_id in {dim_id, name}:
        return True
    if tracker.name and tracker.name in {dim_id, name}:
        return True
    if name and tracker.name and name.lower() == tracker.name.lower():
        return True
    return False


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
