from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from src.config import Settings
from src.services.direct_report_rows import fetch_campaign_performance_rows
from src.services.parallel_fetch import map_parallel
from src.services.rsya_network import CabinetRef, cabinet_refs
from src.vat import cost_with_vat
from src.yandex_direct import YandexDirectClient, _parse_float, _parse_int, conversions_for_goal

logger = logging.getLogger(__name__)

RESULT_TTL_SECONDS = 6 * 60 * 60
BEST_CPA_MIN_CONVERSIONS = 3.0

_TYPE_ALIASES = {
    "КОНВЕРСИИ": "КОНВЕР",
    "КОНВЕРС": "КОНВЕР",
    "КОНВЕР": "КОНВЕР",
    "ПРИЛОЖЕНИЕ": "ПРИЛОЖ",
    "ПРИЛОЖ": "ПРИЛОЖ",
    "ПРИЛ": "ПРИЛОЖ",
}

_STRATEGY_NAMES = {
    "WB_MAXIMUM_CLICKS": "Максимум кликов",
    "WB_MAXIMUM_CONVERSION_RATE": "Максимум конверсий",
    "AVERAGE_CPC": "Средняя цена клика",
    "AVERAGE_CPA": "Средняя цена конверсии",
    "AVERAGE_CPA_MULTIPLE_GOALS": "Средняя цена конверсии",
    "AVERAGE_CRR": "Средняя доля расходов",
    "AVERAGE_ROI": "Средняя рентабельность",
    "PAY_FOR_CONVERSION": "Оплата за конверсии",
    "PAY_FOR_CONVERSION_CRR": "Оплата за конверсии, ДРР",
    "PAY_FOR_CONVERSION_MULTIPLE_GOALS": "Оплата за конверсии",
    "HIGHEST_POSITION": "Ручные ставки",
    "LOWEST_COST": "Низкая цена клика",
    "WEEKLY_CLICK_PACKAGE": "Пакет кликов",
    "SERVING_OFF": "Показы выключены",
    "NETWORK_DEFAULT": "Стандартная стратегия сети",
    "MAX_PROFIT": "Максимум прибыли",
    "MAXIMUM_COVERAGE": "Ручное управление в сетях",
    "WB_MAXIMUM_APP_INSTALLS": "Максимум установок",
    "AVERAGE_CPI": "Средняя цена установки",
    "CP_MAXIMUM_IMPRESSIONS": "Максимум показов",
    "MANUAL_CPM": "Ручная ставка CPM",
}

_PAY_FOR_CONVERSION = {
    "PAY_FOR_CONVERSION",
    "PAY_FOR_CONVERSION_CRR",
    "PAY_FOR_CONVERSION_MULTIPLE_GOALS",
}

UNKNOWN_STRATEGY = "Стратегия не найдена"
UNKNOWN_PAYMENT = "Не определена"
UNKNOWN_BUDGET = "Не определён"


def campaign_type_of(name: str) -> str:
    token = name.strip().upper()
    for sep in (" ", "|", "—", "–", "-", "_", "/", "\\", ".", ":"):
        token = token.split(sep, 1)[0]
    token = token.strip()
    if not token:
        return "ДРУГИЕ"
    if token.startswith("КОНВЕР"):
        return "КОНВЕР"
    if token.startswith("ПРИЛ"):
        return "ПРИЛОЖ"
    mapped = _TYPE_ALIASES.get(token, token)
    if mapped.isalpha() and 2 <= len(mapped) <= 16:
        return mapped
    return "ДРУГИЕ"


def describe_strategy(strategy: dict | None) -> tuple[str, str, str]:
    """Название стратегии, модель оплаты и вид бюджета — как в кабинете Директа."""
    if not strategy:
        return UNKNOWN_STRATEGY, UNKNOWN_PAYMENT, UNKNOWN_BUDGET

    active: list[tuple[str | None, str, str, str]] = []
    if "BiddingStrategyType" in strategy and "Search" not in strategy and "Network" not in strategy:
        name, payment, budget = _describe_placement(strategy)
        if name != "Показы выключены":
            active.append((None, name, payment, budget))
    else:
        for key, side in (("Search", "Поиск"), ("Network", "РСЯ")):
            placement = strategy.get(key)
            if not isinstance(placement, dict):
                continue
            name, payment, budget = _describe_placement(placement)
            if name != "Показы выключены":
                active.append((side, name, payment, budget))

    if not active:
        return "Показы выключены", "Показы выключены", _budget_kind(strategy)

    unique_names = {name for _, name, _, _ in active}
    if len(unique_names) == 1:
        strategy_label = next(iter(unique_names))
    else:
        strategy_label = " · ".join(f"{side}: {name}" for side, name, _, _ in active if side)

    unique_payments = {payment for _, _, payment, _ in active}
    payment_label = next(iter(unique_payments)) if len(unique_payments) == 1 else "Смешанная"

    meaningful = [budget for _, _, _, budget in active if budget != "Бюджет не задан"]
    unique_budgets = set(meaningful)
    if not unique_budgets:
        budget_label = "Бюджет не задан"
    elif len(unique_budgets) == 1:
        budget_label = next(iter(unique_budgets))
    else:
        budget_label = "Разный бюджет"
    return strategy_label, payment_label, budget_label


def _describe_placement(node: dict) -> tuple[str, str, str]:
    code = str(node.get("BiddingStrategyType") or "")
    name = _STRATEGY_NAMES.get(code, code or "Неизвестная стратегия")
    if code == "SERVING_OFF":
        payment = "Показы выключены"
    elif code in _PAY_FOR_CONVERSION:
        payment = "За конверсии"
    elif code:
        payment = "За клики"
    else:
        payment = UNKNOWN_PAYMENT
    return name, payment, _budget_kind(node)


def _budget_kind(node: dict) -> str:
    explicit: list[str] = []
    weekly = False
    period = False

    def walk(obj: object) -> None:
        nonlocal weekly, period
        if not isinstance(obj, dict):
            return
        budget_type = obj.get("BudgetType")
        if budget_type:
            explicit.append(str(budget_type))
        if obj.get("CustomPeriodBudget"):
            period = True
        limit = obj.get("WeeklySpendLimit")
        if isinstance(limit, (int, float)) and limit > 0:
            weekly = True
        for value in obj.values():
            walk(value)

    walk(node)
    if "CUSTOM_PERIOD_BUDGET" in explicit:
        return "Бюджет на период"
    if "WEEKLY_BUDGET" in explicit:
        return "Недельный бюджет"
    if period and not weekly:
        return "Бюджет на период"
    if weekly:
        return "Недельный бюджет"
    return "Бюджет не задан"


def strategies_by_campaign(campaigns: list[dict]) -> dict[int, tuple[str, str, str]]:
    found: dict[int, tuple[str, str, str]] = {}
    for campaign in campaigns:
        raw_id = campaign.get("Id")
        campaign_id = raw_id if isinstance(raw_id, int) else _parse_int(str(raw_id or ""))
        if not campaign_id:
            continue
        block = None
        for key in (
            "TextCampaign",
            "UnifiedCampaign",
            "DynamicTextCampaign",
            "SmartCampaign",
            "MobileAppCampaign",
            "CpmBannerCampaign",
        ):
            candidate = campaign.get(key) or {}
            if isinstance(candidate, dict) and candidate.get("BiddingStrategy"):
                block = candidate["BiddingStrategy"]
                break
        found[campaign_id] = describe_strategy(block if isinstance(block, dict) else None)
    return found


@dataclass(frozen=True)
class CabinetSlice:
    client_name: str
    directologist: str
    spend: float
    clicks: int
    conversions: float
    campaigns: int

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions > 0 else None

    @property
    def cpc(self) -> float | None:
        return self.spend / self.clicks if self.clicks > 0 else None


@dataclass(frozen=True)
class GroupSlice:
    label: str
    spend: float
    clicks: int
    impressions: int
    conversions: float
    campaigns: int
    cabinets: tuple[CabinetSlice, ...]

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions > 0 else None

    @property
    def cpc(self) -> float | None:
        return self.spend / self.clicks if self.clicks > 0 else None


@dataclass
class CampaignFact:
    client_id: int
    client_name: str
    directologist: str
    campaign_type: str
    strategy: str
    payment: str
    budget: str
    spend: float = 0.0
    clicks: int = 0
    impressions: int = 0
    conversions: float = 0.0


@dataclass
class CabinetCampaigns:
    cabinet: CabinetRef
    facts: list[CampaignFact] = field(default_factory=list)
    error: str | None = None
    strategy_error: str | None = None


@dataclass(frozen=True)
class CampaignTypesReport:
    date_from: date
    date_to: date
    by_type: tuple[GroupSlice, ...]
    by_payment: tuple[GroupSlice, ...]
    by_strategy: tuple[GroupSlice, ...]
    by_budget: tuple[GroupSlice, ...]
    total_spend: float
    total_conversions: float
    errors: tuple[tuple[str, str], ...]
    cabinets_total: int


def parse_cabinet_facts(
    rows: list[dict[str, str]],
    strategies: dict[int, tuple[str, str, str]],
    ref: CabinetRef,
    vat_rate: float,
) -> list[CampaignFact]:
    grouped: dict[int | str, CampaignFact] = {}
    for row in rows:
        campaign_id = _parse_int(row.get("CampaignId"))
        name = (row.get("CampaignName") or "").strip() or "—"
        key: int | str = campaign_id or name
        fact = grouped.get(key)
        if fact is None:
            strategy, payment, budget = strategies.get(campaign_id, (UNKNOWN_STRATEGY, UNKNOWN_PAYMENT, UNKNOWN_BUDGET))
            fact = CampaignFact(
                client_id=ref.client_id,
                client_name=ref.client_name,
                directologist=ref.directologist,
                campaign_type=campaign_type_of(name),
                strategy=strategy,
                payment=payment,
                budget=budget,
            )
            grouped[key] = fact
        fact.spend += _parse_float(row.get("Cost", "0"))
        fact.clicks += _parse_int(row.get("Clicks", "0"))
        fact.impressions += _parse_int(row.get("Impressions", "0"))
        fact.conversions += sum(conversions_for_goal(row, gid, ref.attribution_model) for gid in ref.goal_ids)

    facts: list[CampaignFact] = []
    for fact in grouped.values():
        if fact.spend <= 0 and fact.clicks <= 0 and fact.conversions <= 0:
            continue
        fact.spend = cost_with_vat(fact.spend, vat_rate)
        facts.append(fact)
    return facts


def _group(facts: list[CampaignFact], key_name: str) -> tuple[GroupSlice, ...]:
    buckets: dict[str, dict] = {}
    for fact in facts:
        label = getattr(fact, key_name)
        bucket = buckets.setdefault(
            label,
            {"spend": 0.0, "clicks": 0, "impressions": 0, "conversions": 0.0, "campaigns": 0, "clients": {}},
        )
        bucket["spend"] += fact.spend
        bucket["clicks"] += fact.clicks
        bucket["impressions"] += fact.impressions
        bucket["conversions"] += fact.conversions
        bucket["campaigns"] += 1
        client = bucket["clients"].setdefault(
            fact.client_id,
            {
                "client_name": fact.client_name,
                "directologist": fact.directologist,
                "spend": 0.0,
                "clicks": 0,
                "conversions": 0.0,
                "campaigns": 0,
            },
        )
        client["spend"] += fact.spend
        client["clicks"] += fact.clicks
        client["conversions"] += fact.conversions
        client["campaigns"] += 1

    slices: list[GroupSlice] = []
    for label, bucket in buckets.items():
        cabinets = tuple(
            sorted(
                (
                    CabinetSlice(
                        client_name=item["client_name"],
                        directologist=item["directologist"],
                        spend=item["spend"],
                        clicks=item["clicks"],
                        conversions=item["conversions"],
                        campaigns=item["campaigns"],
                    )
                    for item in bucket["clients"].values()
                ),
                key=lambda item: item.conversions,
                reverse=True,
            )
        )
        slices.append(
            GroupSlice(
                label=label,
                spend=bucket["spend"],
                clicks=bucket["clicks"],
                impressions=bucket["impressions"],
                conversions=bucket["conversions"],
                campaigns=bucket["campaigns"],
                cabinets=cabinets,
            )
        )
    slices.sort(key=lambda item: (item.conversions, item.spend), reverse=True)
    return tuple(slices)


def build_campaign_types_report(
    cabinets: list[CabinetCampaigns],
    date_from: date,
    date_to: date,
) -> CampaignTypesReport:
    facts: list[CampaignFact] = []
    errors: list[tuple[str, str]] = []
    for item in cabinets:
        if item.error:
            errors.append((item.cabinet.client_name, item.error))
            continue
        if item.strategy_error:
            errors.append((item.cabinet.client_name, f"Стратегии не загрузились: {item.strategy_error}"))
        facts.extend(item.facts)
    return CampaignTypesReport(
        date_from=date_from,
        date_to=date_to,
        by_type=_group(facts, "campaign_type"),
        by_payment=_group(facts, "payment"),
        by_strategy=_group(facts, "strategy"),
        by_budget=_group(facts, "budget"),
        total_spend=sum(fact.spend for fact in facts),
        total_conversions=sum(fact.conversions for fact in facts),
        errors=tuple(errors),
        cabinets_total=len(cabinets),
    )


def best_labels(slices: tuple[GroupSlice, ...]) -> tuple[str | None, str | None]:
    with_sales = [item for item in slices if item.conversions > 0]
    best_conversions = with_sales[0].label if with_sales else None
    eligible = [item for item in slices if item.conversions >= BEST_CPA_MIN_CONVERSIONS and item.cpa is not None]
    best_cpa = min(eligible, key=lambda item: item.cpa or 0).label if eligible else None
    return best_conversions, best_cpa


def _load_cabinet(settings: Settings, ref: CabinetRef, date_from: date, date_to: date) -> CabinetCampaigns:
    try:
        api = YandexDirectClient(settings.yandex_token, ref.yandex_login)
        rows = fetch_campaign_performance_rows(
            api,
            date_from,
            date_to,
            list(ref.goal_ids),
            ref.attribution_model,
        )
    except Exception as exc:
        logger.warning("Типы кампаний: ошибка кабинета %s: %s", ref.client_name, exc)
        return CabinetCampaigns(cabinet=ref, error=str(exc)[:300])

    strategy_error = None
    strategies: dict[int, tuple[str, str, str]] = {}
    try:
        strategies = strategies_by_campaign(api.fetch_campaign_settings())
    except Exception as exc:
        logger.warning("Типы кампаний: стратегии кабинета %s: %s", ref.client_name, exc)
        strategy_error = str(exc)[:300]
    return CabinetCampaigns(
        cabinet=ref,
        facts=parse_cabinet_facts(rows, strategies, ref, settings.vat_rate),
        strategy_error=strategy_error,
    )


@dataclass
class CampaignTypesJob:
    total: int
    started_at: float = field(default_factory=time.time)
    done: int = 0
    finished_at: float | None = None
    cabinets: list[CabinetCampaigns] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def expired(self) -> bool:
        return self.finished_at is not None and time.time() - self.finished_at > RESULT_TTL_SECONDS


_JOBS: dict[tuple, CampaignTypesJob] = {}
_JOBS_LOCK = threading.Lock()


def _job_key(settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> tuple:
    return (
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        tuple((ref.client_id, ref.goal_ids, ref.attribution_model) for ref in refs),
    )


def _run_job(
    job: CampaignTypesJob,
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
) -> None:
    def _one(ref: CabinetRef) -> CabinetCampaigns:
        result = _load_cabinet(settings, ref, date_from, date_to)
        with _JOBS_LOCK:
            job.done += 1
        return result

    try:
        job.cabinets = map_parallel(_one, refs)
    finally:
        job.finished_at = time.time()


def get_campaign_types_job(
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
    *,
    force: bool = False,
) -> CampaignTypesJob:
    key = _job_key(settings, refs, date_from, date_to)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.running:
            return job
        if job and not force and not job.expired:
            return job
        job = CampaignTypesJob(total=len(refs))
        _JOBS[key] = job
    threading.Thread(
        target=_run_job,
        args=(job, settings, refs, date_from, date_to),
        name="campaign-types",
        daemon=True,
    ).start()
    return job


__all__ = [
    "BEST_CPA_MIN_CONVERSIONS",
    "build_campaign_types_report",
    "best_labels",
    "cabinet_refs",
    "campaign_type_of",
    "describe_strategy",
    "get_campaign_types_job",
]
