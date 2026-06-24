from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

STAT_DATA_URL = "https://api.appmetrica.yandex.com/stat/v1/data"
EVENTS_URL = "https://api.appmetrica.yandex.com/v1/traffic/sources/events"
TRACKERS_URL = "https://api.appmetrica.yandex.com/management/v1/application/{application_id}/trackers"
TRACKERS_URL_ALT = "https://api.appmetrica.yandex.com/management/v1/applications/{application_id}/trackers"
APPLICATIONS_URL = "https://api.appmetrica.yandex.com/management/v1/applications"
LOGS_EXPORT_URL = "https://api.appmetrica.yandex.com/logs/v1/export/{log_type}.json"
LOGS_STATUS_URL = "https://api.appmetrica.yandex.com/logs/v1/export/{request_id}/status.json"
LOGS_DOWNLOAD_URL = (
    "https://api.appmetrica.yandex.com/logs/v1/export/{request_id}/part/{part}/download.json"
)
_LOGS_MAX_WAIT_SECONDS = 60
_LOGS_POLL_INTERVAL = 3
_LOGS_REQUEST_TIMEOUT = 45
_LOGS_CHUNK_DAYS = 7

_INSTALL_DIMENSION_PROBES = (
    ("ym:i:installDevices", "ym:i:trackerName"),
    ("ym:ts:advInstallDevices", "ym:ts:trackerName"),
)
_PURCHASE_DIMENSION_PROBES = (
    ("ym:ts:purchaseEvents", "ym:ts:trackerName"),
    ("ym:ts:inappPurchaseEvents", "ym:ts:trackerName"),
    ("ym:ts:revenueEvents", "ym:ts:trackerName"),
)
_SERVE_HASH_RE = re.compile(r"/serve/(\d+)")

_TRACKER_FILTER_ATTRIBUTES = (
    "ym:i:trackerName",
    "ym:ts:trackerName",
)

_TRACKER_DIMENSIONS_BY_PREFIX: dict[str, tuple[str, ...]] = {
    "ym:i:": ("ym:i:trackerName",),
    "ym:ts:": ("ym:ts:trackerName",),
}

BUILTIN_INSTALL_KEY = "__builtin_install__"
BUILTIN_PURCHASE_KEY = "__builtin_purchase__"
BUILTIN_INSTALL_LABEL = "Установки (трекинг AppMetrica)"
BUILTIN_PURCHASE_LABEL = "Покупки In-App Revenue"

BUILTIN_GOALS = (
    (BUILTIN_INSTALL_KEY, BUILTIN_INSTALL_LABEL),
    (BUILTIN_PURCHASE_KEY, BUILTIN_PURCHASE_LABEL),
)

_TRACKER_DIMENSION = "ym:i:trackerName"


@dataclass(frozen=True)
class ResolvedTracker:
    tracking_id: str
    name: str


class AppMetricaError(RuntimeError):
    pass


class AppMetricaClient:
    def __init__(
        self,
        token: str,
        *,
        application_id_hints: tuple[int, ...] = (),
    ) -> None:
        self._token = (token or "").strip()
        self._application_id_hints = application_id_hints
        self._applications_cache: list[dict] | None = None
        self._install_log_chunk_cache: dict[tuple[int, str, str], list[dict]] = {}
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
        if not serve_hash and not (ref.isdigit() and len(ref) <= 12):
            matched = _match_tracker_in_list(
                self._list_trackers_optional(application_id),
                ref,
                "",
            )
            if matched:
                return application_id, matched
            discovered = self._discover_tracker_by_name(
                application_id,
                ref,
                date_from=date_from,
                date_to=date_to,
            )
            if discovered:
                return application_id, discovered
            return application_id, ResolvedTracker(ref, ref)

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
            guessed = self._try_app_field_as_tracker_id(
                application_id,
                date_from=date_from,
                date_to=date_to,
            )
            if guessed:
                return guessed
            probed = self._try_resolve_by_stat_probe(
                application_id,
                serve_hash,
                date_from=date_from,
                date_to=date_to,
            )
            if probed:
                return probed
            apps = self._candidate_application_ids(application_id)
            raise AppMetricaError(
                f"Не удалось определить трекер по ссылке /serve/{serve_hash}. "
                f"В поле «Трекер» укажите название трекера (например «ЯД Ковров») "
                f"или числовой ID из AppMetrica → Трекинг. "
                f"Проверено приложений: {len(apps)}."
            )

        resolved = self._try_resolve_by_name_stat(
            application_id,
            ref,
            date_from=date_from,
            date_to=date_to,
        )
        if resolved:
            return resolved

        end = date_to or date.today()
        start = date_from or (end - timedelta(days=90))
        for app_id in self._candidate_application_ids(application_id):
            if self._stat_api_accessible(app_id, start, end):
                return app_id, ResolvedTracker(ref, ref)

        raise AppMetricaError(
            f"Трекер «{ref}» не найден. "
            f"Проверьте ID приложения AppMetrica (сейчас {application_id}) и название трекера."
        )

    def _discover_tracker_by_name(
        self,
        application_id: int,
        tracker_ref: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> ResolvedTracker | None:
        end = date_to or date.today()
        start = date_from or (end - timedelta(days=90))
        for probe_metric, tracker_dim in _INSTALL_DIMENSION_PROBES:
            params = {
                "ids": application_id,
                "metrics": probe_metric,
                "dimensions": tracker_dim,
                "date1": start.isoformat(),
                "date2": end.isoformat(),
                "limit": 1000,
                "accuracy": "full",
            }
            try:
                payload = self._get_stat_data(params)
            except AppMetricaError:
                continue

            for row in payload.get("data") or []:
                dimensions = row.get("dimensions") or []
                if not dimensions:
                    continue
                dim = dimensions[0]
                for value in _tracker_dimension_values(dim):
                    if not _tracker_names_match(tracker_ref, value):
                        continue
                    if isinstance(dim, dict):
                        tracking_id = str(dim.get("id", "")).strip()
                        name = str(dim.get("name", "")).strip()
                    else:
                        tracking_id = value
                        name = value
                    key = tracking_id or name or value
                    label = name or tracking_id or value
                    return ResolvedTracker(key, label)
        return None

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

    def _try_app_field_as_tracker_id(
        self,
        maybe_tracker_id: int,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[int, ResolvedTracker] | None:
        tracker_id = str(maybe_tracker_id)
        for app_id in self._candidate_application_ids(0):
            if not self._stat_api_accessible(
                app_id,
                date_from or (date.today() - timedelta(days=90)),
                date_to or date.today(),
            ):
                continue
            for tracker in self._list_trackers_for_app(
                app_id,
                date_from=date_from,
                date_to=date_to,
            ):
                normalized = _normalize_tracker(tracker)
                if tracker_id in {
                    normalized.get("id", ""),
                    normalized.get("name", ""),
                }:
                    name = str(normalized.get("name", "")).strip() or tracker_id
                    return app_id, ResolvedTracker(tracker_id, name)
        return None

    def _try_resolve_by_stat_probe(
        self,
        application_id: int,
        serve_hash: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[int, ResolvedTracker] | None:
        end = date_to or date.today()
        start = date_from or (end - timedelta(days=90))
        probe_values = [serve_hash, str(application_id)]

        for app_id in self._candidate_application_ids(application_id):
            if not self._stat_api_accessible(app_id, start, end):
                continue
            for tracker in self._list_trackers_via_stat(
                app_id,
                date_from=start,
                date_to=end,
            ):
                tracking_id = str(tracker.get("id", "")).strip()
                name = str(tracker.get("name", "")).strip()
                if serve_hash in {tracking_id, name}:
                    return app_id, ResolvedTracker(tracking_id or serve_hash, name or tracking_id)
            for value in probe_values:
                if not value or not self._tracker_filter_works(app_id, value, start, end):
                    continue
                label = value
                for tracker in self._list_trackers_via_stat(app_id, date_from=start, date_to=end):
                    if str(tracker.get("id", "")).strip() == value:
                        label = str(tracker.get("name", "")).strip() or value
                        break
                return app_id, ResolvedTracker(value, label)
        return None

    def _tracker_filter_works(
        self,
        application_id: int,
        tracker_value: str,
        date_from: date,
        date_to: date,
    ) -> bool:
        escaped = _escape_filter_value(tracker_value)
        probe_configs = (
            ("ym:i:installDevices", "ym:i:trackerName"),
            ("ym:ts:advInstallDevices", "ym:ts:trackerName"),
        )
        for metric, attribute in probe_configs:
            try:
                self._fetch_daily_counts_metric(
                    application_id,
                    metric,
                    f"{attribute}=='{escaped}'",
                    date_from,
                    date_to,
                )
                return True
            except AppMetricaError as exc:
                if _is_tracker_filter_rejected(exc):
                    continue
                text = str(exc).lower()
                if "http 404" in text or "не найдено" in text:
                    return False
                raise
        return False

    def _try_resolve_by_name_stat(
        self,
        application_id: int,
        tracker_name: str,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> tuple[int, ResolvedTracker] | None:
        ref = tracker_name.strip()
        if not ref or _extract_serve_hash(ref):
            return None

        end = date_to or date.today()
        start = date_from or (end - timedelta(days=90))

        for app_id in self._candidate_application_ids(application_id):
            if not self._stat_api_accessible(app_id, start, end):
                continue
            if self._tracker_filter_works(app_id, ref, start, end):
                return app_id, ResolvedTracker(ref, ref)

            for tracker in self._list_trackers_via_stat(
                app_id,
                date_from=start,
                date_to=end,
            ):
                name = str(tracker.get("name", "")).strip()
                tracking_id = str(tracker.get("id", "")).strip()
                if not _tracker_names_match(ref, name):
                    continue
                label = name or tracking_id or ref
                key = tracking_id or name or ref
                for filter_value in (name, tracking_id, ref):
                    if filter_value and self._tracker_filter_works(app_id, filter_value, start, end):
                        return app_id, ResolvedTracker(key, label)
                return app_id, ResolvedTracker(key, label)
        return None

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
        for hint in self._application_id_hints:
            if hint and hint not in ids:
                ids.append(hint)
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
        merged: dict[str, dict] = {}
        for tracker in self._list_trackers_optional(application_id):
            normalized = _normalize_tracker(tracker)
            key = str(normalized.get("id", "")) or str(normalized.get("name", ""))
            if key:
                merged[key] = normalized
        for tracker in self._list_trackers_via_stat(
            application_id,
            date_from=date_from,
            date_to=date_to,
        ):
            normalized = _normalize_tracker(tracker)
            key = str(normalized.get("id", "")) or str(normalized.get("name", ""))
            if not key:
                continue
            if key in merged:
                if not merged[key].get("name") and normalized.get("name"):
                    merged[key]["name"] = normalized["name"]
            else:
                merged[key] = normalized
        return list(merged.values())

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

        probe_configs = (
            ("ym:i:installDevices", "ym:i:trackerName"),
            ("ym:ts:advInstallDevices", "ym:ts:trackerName"),
        )
        trackers: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for metrics, dimension in probe_configs:
            params = {
                "ids": application_id,
                "metrics": metrics,
                "dimensions": dimension,
                "date1": start.isoformat(),
                "date2": end.isoformat(),
                "limit": 10000,
                "accuracy": "full",
            }
            try:
                payload = self._get_stat_data(params)
            except AppMetricaError:
                continue

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
            if trackers:
                break
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
            if any(
                marker in text
                for marker in ("http 404", "http 403", "не найдено", "not found", "нет доступа")
            ):
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
            metrics = (
                "ym:i:installDevices",
                "ym:ts:advInstallDevices",
            ) if tracker_ref else (
                "ym:i:installDevices",
                "ym:ts:advInstallDevices",
            )
            return self._fetch_with_metric_fallbacks(
                application_id,
                metrics,
                date_from,
                date_to,
                resolved_tracker=resolved_tracker,
                event_key=event_key,
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
                event_key=event_key,
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
            event_key=event_key,
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
        event_key: str | None = None,
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
                        event_key=event_key,
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
        event_key: str | None = None,
    ) -> dict[date, float]:
        last_error: AppMetricaError | None = None
        empty_result: dict[date, float] | None = None

        if event_key != BUILTIN_PURCHASE_KEY and not metric.startswith("ym:r:"):
            for probe_metric, tracker_dim in _INSTALL_DIMENSION_PROBES:
                try:
                    result = self._fetch_daily_counts_grouped_by_tracker(
                        application_id,
                        probe_metric,
                        tracker_dim,
                        tracker,
                        date_from,
                        date_to,
                        event_filter=event_filter,
                    )
                    if result:
                        return result
                    empty_result = result
                except AppMetricaError as exc:
                    last_error = exc
                    logger.warning(
                        "Tracker dimension %s via %s failed: %s",
                        tracker_dim,
                        probe_metric,
                        exc,
                    )

        if event_key == BUILTIN_PURCHASE_KEY or metric.startswith(("ym:r:", "ym:ts:")):
            for probe_metric, tracker_dim in _PURCHASE_DIMENSION_PROBES:
                try:
                    result = self._fetch_daily_counts_grouped_by_tracker(
                        application_id,
                        probe_metric,
                        tracker_dim,
                        tracker,
                        date_from,
                        date_to,
                        event_filter=event_filter,
                    )
                    if result:
                        return result
                    empty_result = result
                except AppMetricaError as exc:
                    last_error = exc
                    logger.warning(
                        "Purchase tracker dimension %s via %s failed: %s",
                        tracker_dim,
                        probe_metric,
                        exc,
                    )

        prefix = _metric_prefix(metric)
        if prefix in _TRACKER_DIMENSIONS_BY_PREFIX:
            try:
                result = self._fetch_daily_counts_by_tracker_dimension(
                    application_id,
                    metric,
                    tracker,
                    date_from,
                    date_to,
                    event_filter=event_filter,
                )
                if result:
                    return result
                empty_result = result
            except AppMetricaError as exc:
                last_error = exc

        for tracker_filter in _tracker_filter_variants(tracker, metric):
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

        can_use_install_logs = event_key != BUILTIN_PURCHASE_KEY and not metric.startswith("ym:r:")
        if can_use_install_logs:
            try:
                return self._fetch_tracked_daily_counts_from_logs(
                    application_id,
                    tracker,
                    date_from,
                    date_to,
                    event_key=event_key,
                    metric=metric,
                )
            except AppMetricaError as exc:
                last_error = exc

        if event_key == BUILTIN_PURCHASE_KEY or metric.startswith("ym:r:"):
            try:
                return self._fetch_tracked_purchases_from_logs(
                    application_id,
                    tracker,
                    date_from,
                    date_to,
                )
            except AppMetricaError as exc:
                last_error = exc

        if empty_result is not None:
            return empty_result

        available = self._list_tracker_names_from_stat(
            application_id, date_from, date_to
        )
        raise _tracker_filter_error(tracker, last_error, available_names=available)

    def _fetch_daily_counts_grouped_by_tracker(
        self,
        application_id: int,
        metric: str,
        tracker_dim: str,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
        *,
        event_filter: str | None = None,
    ) -> dict[date, float]:
        date_dim = _date_dimension_for_metric(metric)
        params = {
            "ids": application_id,
            "metrics": metric,
            "dimensions": f"{date_dim},{tracker_dim}",
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
        prefix = _metric_prefix(metric)
        tracker_dims = _TRACKER_DIMENSIONS_BY_PREFIX.get(prefix, ())
        if not tracker_dims:
            raise AppMetricaError(
                f"Метрика {metric} не поддерживает группировку по трекеру."
            )

        last_error: AppMetricaError | None = None
        for tracker_dim in tracker_dims:
            try:
                return self._fetch_daily_counts_grouped_by_tracker(
                    application_id,
                    metric,
                    tracker_dim,
                    tracker,
                    date_from,
                    date_to,
                    event_filter=event_filter,
                )
            except AppMetricaError as exc:
                last_error = exc
                continue

        if last_error:
            raise last_error
        raise AppMetricaError(f"Не удалось сгруппировать {metric} по трекеру.")

    def _fetch_tracked_daily_counts_from_logs(
        self,
        application_id: int,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
        *,
        event_key: str | None = None,
        metric: str = "",
    ) -> dict[date, float]:
        if event_key == BUILTIN_PURCHASE_KEY or metric.startswith("ym:r:"):
            raise AppMetricaError(
                "Logs API не поддерживает фильтрацию покупок по трекеру."
            )

        result: dict[date, float] = {}
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + timedelta(days=_LOGS_CHUNK_DAYS - 1), date_to)
            for row in self._fetch_install_log_rows(
                application_id,
                chunk_start,
                chunk_end,
            ):
                if not _logs_row_matches_tracker(row, tracker):
                    continue
                day = _parse_date_value(str(row.get("install_datetime", "")))
                if day is None:
                    continue
                result[day] = result.get(day, 0.0) + 1.0
            chunk_start = chunk_end + timedelta(days=1)
        return result

    def _fetch_tracked_purchases_from_logs(
        self,
        application_id: int,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
    ) -> dict[date, float]:
        installation_ids = self._installation_ids_for_tracker(
            application_id,
            tracker,
            date_from,
            date_to,
        )
        if not installation_ids:
            return {}

        result: dict[date, float] = {}
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + timedelta(days=_LOGS_CHUNK_DAYS - 1), date_to)
            params = {
                "application_id": application_id,
                "date_since": f"{chunk_start.isoformat()} 00:00:00",
                "date_until": f"{chunk_end.isoformat()} 23:59:59",
                "fields": "event_datetime,installation_id",
                "skip_unavailable_shards": "true",
            }
            payload = self._fetch_logs_export("revenue_events", params)
            for row in payload.get("data") or []:
                installation_id = str(row.get("installation_id", "")).strip()
                if installation_id not in installation_ids:
                    continue
                day = _parse_date_value(str(row.get("event_datetime", "")))
                if day is None:
                    continue
                result[day] = result.get(day, 0.0) + 1.0
            chunk_start = chunk_end + timedelta(days=1)
        return result

    def _installation_ids_for_tracker(
        self,
        application_id: int,
        tracker: ResolvedTracker,
        date_from: date,
        date_to: date,
    ) -> set[str]:
        installation_ids: set[str] = set()
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + timedelta(days=_LOGS_CHUNK_DAYS - 1), date_to)
            for row in self._fetch_install_log_rows(
                application_id,
                chunk_start,
                chunk_end,
            ):
                if not _logs_row_matches_tracker(row, tracker):
                    continue
                installation_id = str(row.get("installation_id", "")).strip()
                if installation_id:
                    installation_ids.add(installation_id)
            chunk_start = chunk_end + timedelta(days=1)
        return installation_ids

    def _fetch_install_log_rows(
        self,
        application_id: int,
        date_from: date,
        date_to: date,
    ) -> list[dict]:
        cache_key = (application_id, date_from.isoformat(), date_to.isoformat())
        cached = self._install_log_chunk_cache.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "application_id": application_id,
            "date_since": f"{date_from.isoformat()} 00:00:00",
            "date_until": f"{date_to.isoformat()} 23:59:59",
            "fields": "installation_id,install_datetime,tracker_name,tracking_id",
            "skip_unavailable_shards": "true",
        }
        payload = self._fetch_logs_export("installations", params)
        rows = payload.get("data") or []
        self._install_log_chunk_cache[cache_key] = rows
        return rows

    def _list_tracker_names_from_stat(
        self,
        application_id: int,
        date_from: date,
        date_to: date,
    ) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for probe_metric, tracker_dim in _INSTALL_DIMENSION_PROBES:
            params = {
                "ids": application_id,
                "metrics": probe_metric,
                "dimensions": tracker_dim,
                "date1": date_from.isoformat(),
                "date2": date_to.isoformat(),
                "limit": 100,
                "accuracy": "full",
            }
            try:
                payload = self._get_stat_data(params)
            except AppMetricaError:
                continue
            for row in payload.get("data") or []:
                dimensions = row.get("dimensions") or []
                if not dimensions:
                    continue
                for value in _tracker_dimension_values(dimensions[0]):
                    if value in seen:
                        continue
                    seen.add(value)
                    names.append(value)
        return names

    def _fetch_logs_export(self, log_type: str, params: dict) -> dict:
        url = LOGS_EXPORT_URL.format(log_type=log_type)
        waited = 0

        while waited <= _LOGS_MAX_WAIT_SECONDS:
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers=self._headers(),
                    timeout=_LOGS_REQUEST_TIMEOUT,
                )
            except RequestException as exc:
                raise AppMetricaError("Не удалось подключиться к Logs API AppMetrica") from exc

            if response.status_code == 401:
                raise AppMetricaError("Недействительный токен для API AppMetrica.")
            if response.status_code == 202:
                time.sleep(_LOGS_POLL_INTERVAL)
                waited += _LOGS_POLL_INTERVAL
                continue
            if response.status_code >= 400:
                detail = ""
                try:
                    detail = response.json().get("message", "")
                except Exception:
                    detail = response.text[:200]
                raise AppMetricaError(
                    f"AppMetrica Logs API: HTTP {response.status_code}"
                    + (f" — {detail}" if detail else "")
                )
            return response.json()

        raise AppMetricaError("Превышено время ожидания подготовки выгрузки Logs API.")

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
        request_params = dict(params)
        request_params.setdefault("lang", "ru")
        try:
            response = requests.get(
                STAT_DATA_URL,
                params=request_params,
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
        if self._applications_cache is not None:
            return self._applications_cache
        try:
            response = requests.get(
                APPLICATIONS_URL,
                params={"limit": 1000},
                headers=self._headers(),
                timeout=30,
            )
        except RequestException as exc:
            raise AppMetricaError("Не удалось подключиться к Management API AppMetrica") from exc

        if response.status_code == 401:
            raise AppMetricaError("Недействительный токен для API AppMetrica.")
        if response.status_code >= 400:
            logger.warning("AppMetrica applications list failed: HTTP %s", response.status_code)
            self._applications_cache = []
            return self._applications_cache

        payload = response.json()
        self._applications_cache = _extract_applications(payload)
        return self._applications_cache

    def _list_trackers(self, application_id: int) -> list[dict]:
        trackers: list[dict] = []
        for url_template in (TRACKERS_URL, TRACKERS_URL_ALT):
            url = url_template.format(application_id=application_id)
            batch = self._fetch_trackers_url(url)
            if batch:
                trackers.extend(batch)
        if trackers:
            return trackers
        return self._fetch_trackers_url(TRACKERS_URL.format(application_id=application_id))

    def _fetch_trackers_url(self, url: str) -> list[dict]:
        try:
            response = requests.get(
                url,
                params={"limit": 1000},
                headers=self._headers(),
                timeout=60,
            )
        except RequestException as exc:
            logger.warning("AppMetrica trackers request failed for %s: %s", url, exc)
            return []

        if response.status_code in {401, 403, 404}:
            logger.warning("AppMetrica trackers HTTP %s for %s", response.status_code, url)
            return []
        if response.status_code >= 400:
            logger.warning("AppMetrica trackers HTTP %s for %s", response.status_code, url)
            return []

        return _extract_trackers(response.json())

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
    if "application" in app and isinstance(app["application"], dict):
        app = app["application"]
    for key in ("id", "application_id", "app_id"):
        value = app.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_applications(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("applications", "application", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [value]

    apps: list[dict] = []
    seen: set[int] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            app_id = _application_id_from_payload(obj)
            if app_id is not None and app_id not in seen:
                seen.add(app_id)
                apps.append(obj)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return apps


def _extract_trackers(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [_normalize_tracker(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("trackers", "tracker", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [_normalize_tracker(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            return [_normalize_tracker(value)]

    trackers: list[dict] = []
    seen: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            normalized = _normalize_tracker(obj)
            key = str(normalized.get("id", "")) or str(normalized.get("name", ""))
            raw = normalized.get("_raw")
            if key and key not in seen and isinstance(raw, dict):
                blob = json.dumps(raw, ensure_ascii=False).lower()
                if "tracking" in blob or "tracker" in blob or "serve/" in blob:
                    seen.add(key)
                    trackers.append(normalized)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)
    return trackers


def _normalize_tracker(tracker: dict) -> dict:
    raw = tracker
    if "tracker" in tracker and isinstance(tracker["tracker"], dict):
        raw = tracker["tracker"]

    tracking_id = ""
    for key in ("id", "tracking_id", "tracker_id"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            tracking_id = str(value).strip()
            break

    name = ""
    for key in ("name", "tracker_name", "title"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            name = str(value).strip()
            break

    return {
        "id": tracking_id or name,
        "name": name or tracking_id,
        "_raw": raw,
    }


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

    add(tracker.get("_raw", tracker))
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


def _tracker_names_match(tracker_ref: str, tracker_name: str) -> bool:
    ref = tracker_ref.strip().lower()
    name = tracker_name.strip().lower()
    if not ref or not name:
        return False
    if ref == name:
        return True
    return ref in name or name in ref


def _match_tracker_in_list(
    trackers: list[dict],
    tracker_ref: str,
    serve_hash: str,
) -> ResolvedTracker | None:
    ref = tracker_ref.strip()
    ref_lower = ref.lower()
    for tracker in trackers:
        normalized = _normalize_tracker(tracker) if "_raw" not in tracker else tracker
        tracking_id = str(normalized.get("id", "")).strip()
        name = str(normalized.get("name", "")).strip()
        urls = _tracker_urls_blob(normalized)
        if not tracking_id and not name:
            continue
        if (
            ref == tracking_id
            or ref == name
            or ref_lower == name.lower()
            or _tracker_names_match(ref, name)
        ):
            return ResolvedTracker(tracking_id or name, name or tracking_id)
        if serve_hash and serve_hash in urls:
            return ResolvedTracker(tracking_id or name, name or tracking_id)
    return None


def _extract_serve_hash(tracker_ref: str) -> str:
    match = _SERVE_HASH_RE.search(tracker_ref)
    return match.group(1) if match else ""


def _metric_prefix(metric: str) -> str:
    parts = metric.split(":", 2)
    if len(parts) < 2:
        return ""
    return f"{parts[0]}:{parts[1]}:"


def _filter_attributes_for_metric(metric: str) -> tuple[str, ...]:
    prefix = _metric_prefix(metric)
    if prefix in {"ym:r:", "ym:ce:"}:
        return ("ym:i:trackerName", "ym:ts:trackerName")
    if prefix == "ym:ts:":
        return ("ym:ts:trackerName", "ym:i:trackerName")
    return ("ym:i:trackerName", "ym:ts:trackerName")


def _tracker_filter_values(tracker: ResolvedTracker) -> tuple[str, ...]:
    values: list[str] = []
    if tracker.name:
        values.append(tracker.name)
    tracking_id = (tracker.tracking_id or "").strip()
    if tracking_id and tracking_id != tracker.name and tracking_id.isdigit():
        values.append(tracking_id)
    return tuple(dict.fromkeys(values))


def _tracker_filter_variants(tracker: ResolvedTracker, metric: str = "") -> tuple[str, ...]:
    attributes = _filter_attributes_for_metric(metric) if metric else _TRACKER_FILTER_ATTRIBUTES
    variants: list[str] = []
    for attribute in attributes:
        for value in _tracker_filter_values(tracker):
            variants.append(f"{attribute}=='{_escape_filter_value(value)}'")
    return tuple(dict.fromkeys(variants))


def _is_tracker_filter_rejected(exc: AppMetricaError) -> bool:
    text = str(exc).lower()
    if "4001" in text:
        return True
    if "http 400" not in text and "incorrectly specified" not in text and "неверно указан" not in text:
        return False
    markers = (
        "tracker",
        "tracking",
        "filter",
        "attribute",
        "атрибут",
        "трекер",
    )
    return any(marker in text for marker in markers)


def _tracker_filter_error(
    tracker: ResolvedTracker,
    last_error: AppMetricaError | None,
    *,
    available_names: list[str] | None = None,
) -> AppMetricaError:
    label = tracker.name if tracker.name != tracker.tracking_id else tracker.tracking_id
    message = (
        f"Не удалось отфильтровать данные по трекеру «{label}». "
        "Проверьте, что трекер относится к этому приложению AppMetrica."
    )
    if available_names:
        preview = ", ".join(available_names[:8])
        message += f" Трекеры в отчёте: {preview}."
    if last_error and "Не удалось отфильтровать" not in str(last_error):
        logger.warning("Tracker filter failed for %s: %s", label, last_error)
    return AppMetricaError(message)


def _tracker_dimension_matches(dimension: dict | str, tracker: ResolvedTracker) -> bool:
    values = _tracker_dimension_values(dimension)
    if not values:
        return False
    candidates = [tracker.tracking_id, tracker.name]
    for value in values:
        for candidate in candidates:
            candidate = candidate.strip()
            if not candidate:
                continue
            if value == candidate or value.lower() == candidate.lower():
                return True
            if _tracker_names_match(candidate, value):
                return True
    return False


def _tracker_dimension_values(dimension: dict | str) -> set[str]:
    if isinstance(dimension, dict):
        values: set[str] = set()
        for key in ("name", "id"):
            raw = dimension.get(key)
            if raw is not None:
                text = str(raw).strip()
                if text:
                    values.add(text)
        return values
    text = str(dimension).strip()
    return {text} if text else set()


def _logs_row_matches_tracker(row: dict, tracker: ResolvedTracker) -> bool:
    tracker_name = str(row.get("tracker_name", "")).strip()
    tracking_id = str(row.get("tracking_id", "")).strip()
    candidates = [tracker.tracking_id, tracker.name]
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        if candidate in {tracker_name, tracking_id}:
            return True
        if tracker_name and _tracker_names_match(candidate, tracker_name):
            return True
        if tracking_id and candidate == tracking_id:
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
