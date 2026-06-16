from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import islice


def _chunked(items: list[int], size: int):
    iterator = iter(items)
    while batch := list(islice(iterator, size)):
        yield batch

import requests
from requests.exceptions import ConnectionError as RequestsConnectionError

from src.vat import VAT_RATE, cost_with_vat, cpa_with_vat

logger = logging.getLogger(__name__)

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
LIVE_V4_URL = "https://api.direct.yandex.com/live/v4/json/"
METRIKA_GOALS_URL = "https://api-metrika.yandex.net/management/v1/counter/{counter_id}/goals"

BASE_REPORT_FIELDS = [
    "Date",
    "CampaignName",
    "Impressions",
    "Clicks",
    "Cost",
    "Ctr",
    "AvgCpc",
]

MAX_GOALS_PER_REQUEST = 10

# Модели атрибуции для отчёта: основная + запасные (в UI Директа часто «Автоматическая» = AUTO).
REPORT_ATTRIBUTION_FALLBACK = ("AUTO", "LYDC", "LSC", "LC", "FC", "FCCD", "LSCCD", "LYDCCD")


def _conversion_field(goal_id: int, attribution_model: str) -> str:
    return f"Conversions_{goal_id}_{attribution_model}"


def _attribution_models_for_report(primary: str) -> list[str]:
    order = [primary, *REPORT_ATTRIBUTION_FALLBACK]
    seen: set[str] = set()
    result: list[str] = []
    for model in order:
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result


def conversions_for_goal(row: dict[str, str], goal_id: int, attribution_model: str) -> float:
    """Конверсии по цели с учётом выбранной модели и запасных колонок в TSV."""
    for model in _attribution_models_for_report(attribution_model):
        value = _parse_float(row.get(_conversion_field(goal_id, model), "0"))
        if value > 0:
            return value
    prefix = f"Conversions_{goal_id}_"
    for key, raw in row.items():
        if key.startswith(prefix):
            value = _parse_float(raw)
            if value > 0:
                return value
    return 0.0


def _merge_conversion_columns(
    target: dict[str, str],
    source: dict[str, str],
    goal_ids: list[int],
) -> None:
    for goal_id in goal_ids:
        prefix = f"Conversions_{goal_id}_"
        for key, raw in source.items():
            if not key.startswith(prefix):
                continue
            current = _parse_float(target.get(key, "0"))
            target[key] = str(current + _parse_float(raw))


def _cpa_field(goal_id: int, attribution_model: str) -> str:
    return f"CostPerConversion_{goal_id}_{attribution_model}"


@dataclass(frozen=True)
class GoalInfo:
    goal_id: int
    name: str


@dataclass(frozen=True)
class CampaignStats:
    campaign_name: str
    impressions: int
    clicks: int
    cost: float
    ctr: float
    avg_cpc: float
    conversions: float
    cost_per_conversion: float | None


@dataclass(frozen=True)
class DailyStats:
    report_date: date
    campaigns: tuple[CampaignStats, ...]

    @property
    def impressions(self) -> int:
        return sum(c.impressions for c in self.campaigns)

    @property
    def clicks(self) -> int:
        return sum(c.clicks for c in self.campaigns)

    @property
    def cost(self) -> float:
        return sum(c.cost for c in self.campaigns)

    @property
    def conversions(self) -> float:
        return sum(c.conversions for c in self.campaigns)

    @property
    def ctr(self) -> float:
        if self.impressions == 0:
            return 0.0
        return (self.clicks / self.impressions) * 100

    @property
    def avg_cpc(self) -> float:
        if self.clicks == 0:
            return 0.0
        return self.cost / self.clicks

    @property
    def cost_per_conversion(self) -> float | None:
        if self.conversions == 0:
            return None
        return self.cost / self.conversions


class YandexDirectError(Exception):
    pass


class MetrikaApiError(YandexDirectError):
    pass


class YandexDirectClient:
    def __init__(
        self,
        token: str,
        client_login: str | None = None,
        metrika_token: str | None = None,
    ) -> None:
        self._token = token
        self._client_login = client_login
        self._metrika_token = metrika_token or token

    def fetch_period_stats(
        self,
        date_from: date,
        date_to: date,
        goal_ids: list[int] | None = None,
        attribution_model: str = "LSC",
        vat_rate: float = VAT_RATE,
    ) -> dict[date, DailyStats]:
        goal_ids = goal_ids or []
        if not goal_ids:
            rows = self._fetch_report(date_from, date_to, [], attribution_model)
            return self._group_rows_by_date(rows, vat_rate, attribution_model=attribution_model)

        merged_rows: dict[tuple[date, str], dict[str, str]] = {}
        for chunk in _chunked(goal_ids, MAX_GOALS_PER_REQUEST):
            chunk_rows = self._fetch_report(date_from, date_to, list(chunk), attribution_model)
            self._merge_goal_rows(merged_rows, chunk_rows, list(chunk), attribution_model)

        flat_rows = list(merged_rows.values())
        return self._group_rows_by_date(
            flat_rows, vat_rate, goal_ids, attribution_model=attribution_model
        )

    def list_campaign_ids(self) -> list[int]:
        body = {
            "method": "get",
            "params": {
                "SelectionCriteria": {"States": ["ON", "SUSPENDED", "OFF"]},
                "FieldNames": ["Id"],
            },
        }
        result = self._post_json(CAMPAIGNS_URL, body)
        campaigns = result.get("result", {}).get("Campaigns", [])
        return [int(c["Id"]) for c in campaigns]

    def fetch_goals_from_campaigns(self, campaign_ids: list[int] | None = None) -> list[GoalInfo]:
        if campaign_ids is None:
            campaign_ids = self.list_campaign_ids()
        if not campaign_ids:
            return []

        seen: dict[int, GoalInfo] = {}
        for chunk in _chunked(campaign_ids, 100):
            body = {
                "method": "GetStatGoals",
                "param": {"CampaignIDS": list(chunk)},
            }
            response = self._post_live_v4(body)
            for item in response.get("data", []):
                goal_id = int(item["GoalID"])
                if goal_id not in seen:
                    seen[goal_id] = GoalInfo(goal_id=goal_id, name=str(item.get("Name", f"Цель {goal_id}")))
        return list(seen.values())

    def fetch_goals_from_metrika(self, counter_id: int) -> list[GoalInfo]:
        headers = {
            "Authorization": f"OAuth {self._metrika_token}",
            "Accept": "application/json",
        }
        url = METRIKA_GOALS_URL.format(counter_id=counter_id)
        try:
            response = requests.get(url, headers=headers, timeout=60)
        except RequestsConnectionError as exc:
            raise MetrikaApiError("Не удалось подключиться к API Яндекс.Метрики") from exc

        if response.status_code == 403:
            raise MetrikaApiError(
                "Токен не имеет доступа к API Метрики. "
                "Получите OAuth-токен с правом metrika:read или оставьте поле "
                "«ID счётчика Метрики» пустым — цели загрузятся из Директа."
            )
        if response.status_code == 401:
            raise MetrikaApiError(
                "Недействительный OAuth-токен для Метрики. "
                "Укажите YANDEX_METRIKA_TOKEN в .env или обновите токен."
            )
        if response.status_code != 200:
            raise MetrikaApiError(f"Ошибка API Метрики (HTTP {response.status_code}): {response.text}")

        goals = response.json().get("goals", [])
        return [GoalInfo(goal_id=int(g["id"]), name=str(g.get("name", f"Цель {g['id']}"))) for g in goals]

    def _fetch_report(
        self,
        date_from: date,
        date_to: date,
        goal_ids: list[int],
        attribution_model: str,
    ) -> list[dict[str, str]]:
        field_names = list(BASE_REPORT_FIELDS)
        # В FieldNames — только базовые имена; API сам развернёт их в
        # Conversions_<goalId>_<model> в заголовках TSV при указании Goals.
        field_names.extend(["Conversions", "CostPerConversion"])

        params: dict = {
            "SelectionCriteria": {
                "DateFrom": date_from.isoformat(),
                "DateTo": date_to.isoformat(),
            },
            "FieldNames": field_names,
            "ReportName": f"DailyAnalytics_{date_from}_{date_to}_{int(time.time())}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "NO",
            "IncludeDiscount": "NO",
        }
        if goal_ids:
            params["Goals"] = [str(g) for g in goal_ids]
            params["AttributionModels"] = _attribution_models_for_report(attribution_model)

        body = {"params": params}
        headers = self._build_report_headers()
        payload = json.dumps(body, ensure_ascii=False)

        while True:
            try:
                response = requests.post(
                    REPORTS_URL,
                    data=payload.encode("utf-8"),
                    headers=headers,
                    timeout=120,
                )
            except RequestsConnectionError as exc:
                raise YandexDirectError("Не удалось подключиться к API Яндекс.Директ") from exc

            request_id = response.headers.get("RequestId", "unknown")

            if response.status_code == 200:
                return self._parse_tsv(response.text)

            if response.status_code in (201, 202):
                retry_in = int(response.headers.get("retryIn", 30))
                logger.info(
                    "Отчёт в очереди (HTTP %s), повтор через %s сек. RequestId=%s",
                    response.status_code,
                    retry_in,
                    request_id,
                )
                time.sleep(retry_in)
                continue

            self._raise_api_error(response)

    def _post_json(self, url: str, body: dict) -> dict:
        headers = self._build_api_headers()
        try:
            response = requests.post(url, json=body, headers=headers, timeout=60)
        except RequestsConnectionError as exc:
            raise YandexDirectError("Не удалось подключиться к API Яндекс.Директ") from exc

        if response.status_code != 200:
            self._raise_api_error(response)
        payload = response.json()
        if "error" in payload:
            raise YandexDirectError(f"Ошибка API: {payload['error']}")
        return payload

    def _post_live_v4(self, body: dict) -> dict:
        last_error: YandexDirectError | None = None
        for auth_value in (f"Bearer {self._token}", f"OAuth {self._token}"):
            headers = self._build_api_headers()
            headers["Authorization"] = auth_value
            try:
                response = requests.post(LIVE_V4_URL, json=body, headers=headers, timeout=60)
            except RequestsConnectionError as exc:
                raise YandexDirectError("Не удалось подключиться к Live API v4") from exc

            if response.status_code != 200:
                self._raise_api_error(response)
            payload = response.json()
            error_code = payload.get("error_code")
            if error_code:
                last_error = YandexDirectError(f"Live API error: {payload}")
                if error_code == 53 and auth_value.startswith("Bearer"):
                    continue
                raise last_error
            return payload
        if last_error:
            raise last_error
        raise YandexDirectError("Live API error: не удалось авторизоваться")

    def _build_api_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept-Language": "ru",
            "Content-Type": "application/json; charset=utf-8",
        }
        if self._client_login:
            login = self._client_login.strip()
            try:
                login.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise YandexDirectError(
                    f"Логин кабинета «{login}» содержит кириллицу или недопустимые символы. "
                    "Укажите латинский логин из Яндекс.Директ (поле «Логин кабинета» при редактировании клиента)."
                ) from exc
            headers["Client-Login"] = login
        return headers

    def _build_report_headers(self) -> dict[str, str]:
        headers = self._build_api_headers()
        headers["Accept-Language"] = "en"
        headers.update(
            {
                "skipReportHeader": "true",
                "skipReportSummary": "true",
                "returnMoneyInMicros": "false",
            }
        )
        return headers

    def _raise_api_error(self, response: requests.Response) -> None:
        request_id = response.headers.get("RequestId", "unknown")
        try:
            details = response.json()
        except ValueError:
            details = response.text
        raise YandexDirectError(
            f"Ошибка API (HTTP {response.status_code}, RequestId={request_id}): {details}"
        )

    @staticmethod
    def _parse_tsv(content: str) -> list[dict[str, str]]:
        if not content.strip():
            return []
        reader = csv.DictReader(io.StringIO(content), delimiter="\t")
        return [dict(row) for row in reader if any(row.values())]

    @staticmethod
    def _merge_goal_rows(
        merged: dict[tuple[date, str], dict[str, str]],
        rows: list[dict[str, str]],
        goal_ids: list[int],
        attribution_model: str,
    ) -> None:
        for row in rows:
            key = (_parse_date(row["Date"]), row.get("CampaignName", "—").strip() or "—")
            if key not in merged:
                merged[key] = dict(row)
                continue
            target = merged[key]
            _merge_conversion_columns(target, row, goal_ids)

    def _group_rows_by_date(
        self,
        rows: list[dict[str, str]],
        vat_rate: float,
        goal_ids: list[int] | None = None,
        attribution_model: str = "LSC",
    ) -> dict[date, DailyStats]:
        by_date: dict[date, list[CampaignStats]] = {}
        for row in rows:
            parsed_date = _parse_date(row["Date"])
            campaign = _row_to_campaign(row, vat_rate, goal_ids or [], attribution_model)
            by_date.setdefault(parsed_date, []).append(campaign)
        return {
            day: DailyStats(report_date=day, campaigns=tuple(campaigns))
            for day, campaigns in sorted(by_date.items())
        }


def _parse_date(value: str) -> date:
    return date.fromisoformat(value.strip())


def _row_to_campaign(
    row: dict[str, str],
    vat_rate: float,
    goal_ids: list[int],
    attribution_model: str = "LSC",
) -> CampaignStats:
    cost_raw = _parse_float(row.get("Cost", "0"))
    cost = cost_with_vat(cost_raw, vat_rate)
    clicks = _parse_int(row.get("Clicks", "0"))

    if goal_ids:
        conversions = sum(
            conversions_for_goal(row, gid, attribution_model)
            for gid in goal_ids
        )
    else:
        conversions = _parse_float(row.get("Conversions", "0"))

    avg_cpc_raw = _parse_float(row.get("AvgCpc", "0"))
    avg_cpc = cost_with_vat(avg_cpc_raw, vat_rate) if avg_cpc_raw else (cost / clicks if clicks else 0.0)
    cpa = cpa_with_vat(cost_raw, conversions, vat_rate)

    return CampaignStats(
        campaign_name=row.get("CampaignName", "—").strip() or "—",
        impressions=_parse_int(row.get("Impressions", "0")),
        clicks=clicks,
        cost=cost,
        ctr=_parse_float(row.get("Ctr", "0")),
        avg_cpc=avg_cpc,
        conversions=conversions,
        cost_per_conversion=cpa,
    )


def _parse_int(value: str) -> int:
    cleaned = (value or "0").replace(" ", "").replace(",", ".")
    if not cleaned or cleaned == "--":
        return 0
    return int(float(cleaned))


def _parse_float(value: str) -> float:
    cleaned = (value or "0").replace(" ", "").replace(",", ".")
    if not cleaned or cleaned == "--":
        return 0.0
    return float(cleaned)


def yesterday_and_day_before(reference: date | None = None) -> tuple[date, date]:
    today = reference or date.today()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)
    return yesterday, day_before
