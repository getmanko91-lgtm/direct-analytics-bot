from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

STAT_DATA_URL = "https://api.appmetrica.yandex.com/stat/v1/data"
EVENTS_URL = "https://api.appmetrica.yandex.com/v1/traffic/sources/events"
TRACKERS_URL = "https://api.appmetrica.yandex.com/management/v1/application/{application_id}/trackers"
APPLICATIONS_URL = "https://api.appmetrica.yandex.com/management/v1/applications"
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
            raise AppMetricaError(
                f"Приложение AppMetrica {application_id} не найдено. "
                "Проверьте поле «ID приложения» в карточке клиента — это ID из "
                "Настроек приложения AppMetrica, а не ID трекера из раздела «Трекинг»."
            )
        if response.status_code >= 400:
            raise AppMetricaError(f"AppMetrica API: HTTP {response.status_code}")

        payload = response.json()
        events_info = payload.get("events_info") or {}
        events = events_info.get("events") or []
        return sorted({str(name).strip() for name in events if str(name).strip()})

    def resolve_application_and_tracker(
        self,
        application_id: int,
        tracker_ref: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[int, ResolvedTracker]:
        ref = tracker_ref.strip()
        if not ref:
            raise AppMetricaError("Не задан трекер AppMetrica.")

        serve_hash = _extract_serve_hash(ref)

        swapped = self._find_tracker_id_used_as_application_id(
            application_id,
            date_from=date_from,
            date_to=date_to,
        )
        if swapped:
            return swapped

        for app_id in self._candidate_application_ids(application_id):
            resolved = self._find_tracker_in_app(
                app_id,
                ref,
                serve_hash,
                date_from=date_from,
                date_to=date_to,
            )
            if resolved:
                return app_id, resolved

        if ref.isdigit() and len(ref) <= 12 and not serve_hash:
            app_id = self._find_application_for_tracker_id(
                ref,
                date_from=date_from,
                date_to=date_to,
            )
            return app_id or application_id, ResolvedTracker(ref, ref)

        if serve_hash:
            raise AppMetricaError(
                f"Трекер с ссылкой /serve/{serve_hash} не найден среди доступных приложений. "
                f"Проверьте ID приложения (сейчас {application_id}) и токен AppMetrica."
            )
        raise AppMetricaError(
            f"Трекер «{ref}» не найден. "
            f"Проверьте ID приложения AppMetrica (сейчас {application_id}) и трекинговую ссылку."
        )

    def resolve_tracker(
        self,
        application_id: int,
        tracker_ref: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ResolvedTracker:
        _, tracker = self.resolve_application_and_tracker(
            application_id,
            tracker_ref,
            date_from=date_from,
            date_to=date_to,
        )
        return tracker

    def _find_tracker_in_app(
        self,
        application_id: int,
        tracker_ref: str,
        serve_hash: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ResolvedTracker | None:
        return _match_tracker_in_list(
            self._list_trackers_for_app(
                application_id,
                date_from=date_from,
                date_to=date_to,
            ),
            tracker_ref,
            serve_hash,
        )

    def _find_application_for_tracker_id(
        self,
        tracker_id: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> int | None:
        for app_id in self._candidate_application_ids(0):
            for tracker in self._list_trackers_for_app(
                app_id,
                date_from=date_from,
                date_to=date_to,
            ):
                if str(tracker.get("id", "")).strip() == tracker_id:
                    return app_id
        return None

    def _find_tracker_id_used_as_application_id(
        self,
        application_id: int,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[int, ResolvedTracker] | None:
        tracker_id = str(application_id)
        for app_id in self._candidate_application_ids(application_id):
            for tracker in self._list_trackers_for_app(
                app_id,
                date_from=date_from,
                date_to=date_to,
            ):
                if str(tracker.get("id", "")).strip() == tracker_id:
                    name = str(tracker.get("name", "")).strip() or tracker_id
                    return app_id, ResolvedTracker(tracker_id, name)
        return None

    def _candidate_application_ids(self, preferred_id: int) -> list[int]:
        ids: list[int] = []
        if preferred_id:
            ids.append(preferred_id)
        for app in self._list_applications():
            app_id = _application_id_from_payload(app)
            if app_id is not None and app_id not in ids:
                ids.append(app_id)
        return ids

    def _list_trackers_for_app(
        self,
        application_id: int,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict]:
        trackers = self._list_trackers_optional(application_id)
        if trackers:
            return trackers
        return self._list_trackers_via_stat(
            application_id,
            date_from=date_from,
            date_to=date_to,
        )

    def _list_trackers_via_stat(
        self,
        application_id: int,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict]:
        end = date_to or date.today()
        start = date_from or (end - timedelta(days=90))
        if not self._stat_api_accessible(application_id, start, end):
            return []

        params = {
            "ids": application_id,
            "metrics": "ym:ts:advInstallDevices",
            "dimensions": "ym:ts:tracker",
            "date1": start.isoformat(),
            "date2": end.isoformat(),
            "limit": 10000,
            "accuracy": "full",
        }
        try:
            payload = self._get_stat_data(params)
        except AppMetricaError:
            return []

        trackers: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for row in payload.get("data") or []:
            dimensions = row.get("dimensions") or []
            if not dimensions:
                continue
            dim = dimensions[0]
            if isinstance(dim, dict):
                tracking_id = str(dim.get("id", "")).strip()
                name = str(dim.get("name", "")).strip()
            else:
                tracking_id = str(dim).strip()
                name = tracking_id
            if not tracking_id and not name:
                continue
            key = (tracking_id, name)
            if key in seen:
                continue
            seen.add(key)
            trackers.append(
                {
                    "id": tracking_id or name,
                    "name": name or tracking_id,
                }
            )
        return trackers

    def _stat_api_accessible(self, application_id: int, date_from: date, date_to: date) -> bool:
        try:
            self._get_stat_data(
                {
                    "ids": application_id,
                    "metrics": "ym:i:installDevices",
                    "dimensions": "ym:i:date",
                    "date1": date_from.isoformat(),
                    "date2": date_to.isoformat(),
                    "limit": 1,
                    "accuracy": "full",
                }
            )
            return True
        except AppMetricaError as exc:
            text = str(exc).lower()
            if "http 404" in text or "не найдено" in text or "not found" in text:
                return False
            raise

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
        resolved_tracker: ResolvedTracker | None = None
        if tracker_ref:
            application_id, resolved_tracker = self.resolve_application_and_tracker(
                application_id,
                tracker_ref,
                date_from=date_from,
                date_to=date_to,
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

    def _list_trackers_optional(self, application_id: int) -> list[dict]:
        try:
            return self._list_trackers(application_id)
        except AppMetricaError as exc:
            if "не найдено" in str(exc) or "HTTP 404" in str(exc):
                logger.warning("AppMetrica trackers unavailable for app %s: %s", application_id, exc)
                return []
            raise

    def _list_applications(self) -> list[dict]:
        try:
            response = requests.get(
                APPLICATIONS_URL,
                params={"limit": 1000},
                headers=self._headers(),
                timeout=60,
            )
        except RequestException as exc:
            raise AppMetricaError("Не удалось подключиться к Management API AppMetrica") from exc

        if response.status_code == 401:
            raise AppMetricaError("Недействительный токен для API AppMetrica.")
        if response.status_code >= 400:
            logger.warning("AppMetrica applications list failed: HTTP %s", response.status_code)
            return []

        payload = response.json()
        applications = payload.get("applications")
        if isinstance(applications, list):
            return applications
        if isinstance(payload, list):
            return payload
        return []

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
            logger.warning("AppMetrica trackers 404 for application %s", application_id)
            return []
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


def _application_id_from_payload(app: dict) -> int | None:
    for key in ("id", "application_id", "app_id"):
        value = app.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _tracker_urls_blob(tracker: dict) -> str:
    parts: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for item in value.values():
                add(item)

    for key in (
        "tracking_url",
        "url",
        "click_url",
        "tracking_link",
        "impression_url",
        "tracking_urls",
        "urls",
    ):
        add(tracker.get(key))
    return " ".join(parts)


def _match_tracker_in_list(
    trackers: list[dict],
    tracker_ref: str,
    serve_hash: str,
) -> ResolvedTracker | None:
    ref = tracker_ref.strip()
    ref_lower = ref.lower()
    for tracker in trackers:
        tracking_id = str(tracker.get("id", "")).strip()
        name = str(tracker.get("name", "")).strip()
        urls = _tracker_urls_blob(tracker)
        if not tracking_id:
            continue
        if ref == tracking_id or ref == name or ref_lower == name.lower():
            return ResolvedTracker(tracking_id, name or tracking_id)
        if serve_hash and serve_hash in urls:
            return ResolvedTracker(tracking_id, name or tracking_id)
    return None


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
