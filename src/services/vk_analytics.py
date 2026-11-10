"""Сводка VK Рекламы по всем кабинетам агентства: кампании, тексты, аудитории."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from src.config import Settings
from src.services.parallel_fetch import map_parallel
from src.services.report_store import RESULT_TTL_SECONDS, begin_collection, remember_partial, save_finished
from src.vat import cost_with_vat
from src.vk_ads import VkAdsClient, VkAdsError

logger = logging.getLogger(__name__)

KIND = "vk-analytics"
TITLE_KEYS = ("title_40_vkads", "title_40", "title_30_additional", "title_25", "title_20", "title")
BODY_KEYS = ("text_2000", "text_220", "text_90", "text_50", "text")
STATUS_LABELS = {
    "active": "активен",
    "blocked": "заблокирован",
    "deleted": "удалён",
}


@dataclass
class VkCampaign:
    plan_id: int
    name: str
    objective: str
    status: str
    spend: float
    shows: int
    clicks: int
    goals: float


@dataclass
class VkText:
    banner_id: int
    title: str
    body: str
    status: str
    spend: float
    shows: int
    clicks: int
    goals: float


@dataclass
class VkAudience:
    name: str
    kind: str
    users: int
    created: str
    status: str


@dataclass
class VkCabinet:
    client_id: int
    name: str
    username: str
    status: str
    balance: float
    spend: float
    shows: int
    clicks: int
    goals: float
    campaigns: list[VkCampaign] = field(default_factory=list)
    texts: list[VkText] = field(default_factory=list)
    audiences: list[VkAudience] = field(default_factory=list)
    error: str | None = None

    @property
    def active_spend(self) -> bool:
        return self.spend > 0 or self.clicks > 0 or self.shows > 0 or self.goals > 0


@dataclass
class VkJob:
    total: int
    started_at: float = field(default_factory=time.time)
    done: int = 0
    finished_at: float | None = None
    cabinets: list[VkCabinet] = field(default_factory=list)
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def expired(self) -> bool:
        return self.finished_at is not None and time.time() - self.finished_at > RESULT_TTL_SECONDS


_JOBS: dict[tuple, VkJob] = {}
_JOBS_LOCK = threading.Lock()


def goals_of(base: dict) -> float:
    vk = base.get("vk")
    if isinstance(vk, dict):
        return _num(vk.get("goals"))
    return _num(base.get("goals"))


def metrics_of(payload: dict) -> tuple[float, int, int, float]:
    total = payload.get("total") if isinstance(payload, dict) else None
    base = total.get("base") if isinstance(total, dict) else None
    if not isinstance(base, dict):
        base = payload.get("base") if isinstance(payload, dict) else None
    if not isinstance(base, dict):
        return 0.0, 0, 0, 0.0
    return _num(base.get("spent")), int(_num(base.get("shows"))), int(_num(base.get("clicks"))), goals_of(base)


def banner_copy(textblocks: object) -> tuple[str, str]:
    if not isinstance(textblocks, dict):
        return "", ""
    title = ""
    for key in TITLE_KEYS:
        title = _block_text(textblocks.get(key))
        if title:
            break
    if not title:
        for key, block in textblocks.items():
            if str(key).startswith("title"):
                title = _block_text(block)
                if title:
                    break
    body = ""
    for key in BODY_KEYS:
        body = _block_text(textblocks.get(key))
        if body and body != title:
            break
    if not body:
        for key, block in textblocks.items():
            if str(key).startswith("text"):
                piece = _block_text(block)
                if piece and piece != title:
                    body = piece
                    break
    return title, body


def text_identity(title: str, body: str, banner_id: int) -> str:
    compact = " ".join(f"{title} {body}".casefold().split())
    return compact or f"#{banner_id}"


def build_vk_report(cabinets: list[VkCabinet]) -> dict:
    campaigns = []
    texts: dict[str, dict] = {}
    audiences = []
    for cabinet in cabinets:
        for plan in cabinet.campaigns:
            campaigns.append((cabinet, plan))
        for ad in cabinet.texts:
            key = text_identity(ad.title, ad.body, ad.banner_id)
            bucket = texts.setdefault(
                key,
                {"title": ad.title, "body": ad.body, "deleted": ad.status == "deleted", "shares": {}},
            )
            if not bucket["title"] and ad.title:
                bucket["title"] = ad.title
                bucket["body"] = ad.body
            if ad.status != "deleted":
                bucket["deleted"] = False
            share = bucket["shares"].setdefault(
                cabinet.client_id,
                {"name": cabinet.name, "spend": 0.0, "clicks": 0, "goals": 0.0},
            )
            share["spend"] += ad.spend
            share["clicks"] += ad.clicks
            share["goals"] += ad.goals
        for audience in cabinet.audiences:
            audiences.append((cabinet, audience))
    campaigns.sort(key=lambda item: (-item[1].goals, -item[1].spend))
    ranked_texts = []
    for bucket in texts.values():
        shares = sorted(bucket["shares"].values(), key=lambda share: (-share["goals"], -share["spend"]))
        spend = sum(share["spend"] for share in shares)
        clicks = sum(share["clicks"] for share in shares)
        goals = sum(share["goals"] for share in shares)
        title = bucket["title"] or ("Объявление удалено" if bucket["deleted"] else "Текст недоступен")
        ranked_texts.append(
            {
                "title": title,
                "body": bucket["body"],
                "deleted": title == "Объявление удалено" and not bucket["body"],
                "spend": spend,
                "clicks": clicks,
                "goals": goals,
                "shares": shares,
            }
        )
    ranked_texts.sort(key=lambda item: (-item["goals"], -item["spend"]))
    audiences.sort(key=lambda item: (-item[1].users, item[0].name.casefold(), item[1].name.casefold()))
    ordered = sorted(cabinets, key=lambda item: (-item.goals, -item.spend, item.name.casefold()))
    return {
        "cabinets": ordered,
        "campaigns": campaigns,
        "texts": ranked_texts,
        "audiences": audiences,
        "total_spend": sum(item.spend for item in cabinets),
        "total_goals": sum(item.goals for item in cabinets),
        "total_clicks": sum(item.clicks for item in cabinets),
        "with_sales": sum(1 for item in cabinets if item.goals > 0),
    }


def get_vk_job(settings: Settings, date_from: date, date_to: date, *, force: bool = False) -> VkJob:
    if not settings.vk_configured:
        return VkJob(total=0, done=0, finished_at=time.time(), error="В .env не заданы VK_ADS_CLIENT_ID и VK_ADS_CLIENT_SECRET")
    key = (settings.vk_ads_agency_id, settings.vk_ads_client_id, date_from.isoformat(), date_to.isoformat(), "vk-v1")
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.running:
            return job
        if job and not force and not job.expired:
            return job
        finished, partial = begin_collection(KIND, key, force=force, ttl_seconds=RESULT_TTL_SECONDS)
        if finished is not None:
            finished_at, cabinets = finished
            job = VkJob(total=len(cabinets), done=len(cabinets), finished_at=finished_at, cabinets=cabinets)
            _JOBS[key] = job
            return job
        job = VkJob(total=0, done=len(partial), cabinets=list(partial.values()))
        _JOBS[key] = job
    threading.Thread(
        target=_run_job,
        args=(job, settings, date_from, date_to, key, partial),
        name="vk-analytics",
        daemon=True,
    ).start()
    return job


def _run_job(job: VkJob, settings: Settings, date_from: date, date_to: date, key: tuple, partial: dict) -> None:
    try:
        api = VkAdsClient(settings.vk_ads_client_id, settings.vk_ads_client_secret, settings.vk_ads_agency_id)
        cabinets = _agency_overview(api, date_from, date_to, settings.vat_rate)
        pending = [cabinet for cabinet in cabinets if cabinet.client_id not in partial and cabinet.active_spend]
        for cabinet in cabinets:
            saved = partial.get(cabinet.client_id)
            if isinstance(saved, VkCabinet):
                cabinet.campaigns = saved.campaigns
                cabinet.texts = saved.texts
                cabinet.audiences = saved.audiences
                cabinet.error = saved.error
        with _JOBS_LOCK:
            job.total = len(pending) + len(partial)
            job.cabinets = cabinets
        if pending:
            map_parallel(
                lambda cabinet: _load_details(api, cabinet, date_from, date_to, settings.vat_rate, job, key),
                pending,
                max_workers=3,
            )
    except Exception as exc:
        logger.exception("VK: сбор прервался")
        job.error = str(exc)[:300]
        job.finished_at = time.time()
        return
    job.finished_at = time.time()
    save_finished(KIND, key, job.cabinets, job.finished_at)


def _agency_overview(api: VkAdsClient, date_from: date, date_to: date, vat_rate: float) -> list[VkCabinet]:
    token = api.agency_token()
    clients = {
        int(user["id"]): user
        for user in (
            item.get("user")
            for item in api.get_paged(token, "agency/clients", limit=50)
        )
        if isinstance(user, dict) and user.get("id")
    }
    stats = api.get(
        token,
        "statistics/users/summary",
        {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "metrics": "base"},
    )
    by_id = {}
    for item in stats.get("items") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        spend, shows, clicks, goals = metrics_of(item)
        by_id[int(item["id"])] = (cost_with_vat(spend, vat_rate), shows, clicks, goals)
    ids = list(dict.fromkeys([*clients.keys(), *by_id.keys()]))
    cabinets = []
    for client_id in ids:
        user = clients.get(client_id) or {}
        account = user.get("account") if isinstance(user.get("account"), dict) else {}
        spend, shows, clicks, goals = by_id.get(client_id, (0.0, 0, 0, 0.0))
        cabinets.append(
            VkCabinet(
                client_id=client_id,
                name=_clean(user.get("client_username") or user.get("username") or f"Кабинет {client_id}"),
                username=_clean(user.get("username")),
                status=_clean(user.get("status")),
                balance=_num((account or {}).get("balance")),
                spend=spend,
                shows=shows,
                clicks=clicks,
                goals=goals,
            )
        )
    return cabinets


def _load_details(
    api: VkAdsClient,
    cabinet: VkCabinet,
    date_from: date,
    date_to: date,
    vat_rate: float,
    job: VkJob,
    key: tuple,
) -> VkCabinet:
    try:
        token = api.client_token(cabinet.client_id)
        period = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "metrics": "base"}
        plans = api.get_paged(token, "ad_plans", {"fields": "id,name,status,objective"}, limit=50)
        plan_stats = _stats_by_id(api, token, "statistics/ad_plans/summary", period)
        for plan in plans:
            plan_id = int(plan.get("id") or 0)
            if not plan_id:
                continue
            spend, shows, clicks, goals = plan_stats.get(plan_id, (0.0, 0, 0, 0.0))
            if spend <= 0 and clicks <= 0 and goals <= 0:
                continue
            cabinet.campaigns.append(
                VkCampaign(
                    plan_id=plan_id,
                    name=_clean(plan.get("name") or f"Кампания {plan_id}"),
                    objective=_clean(plan.get("objective")),
                    status=_clean(plan.get("status")),
                    spend=cost_with_vat(spend, vat_rate),
                    shows=shows,
                    clicks=clicks,
                    goals=goals,
                )
            )
        banners = api.get_paged(token, "banners", {"fields": "id,name,status,textblocks"}, limit=50)
        banner_stats = _stats_by_id(api, token, "statistics/banners/summary", period)
        for banner in banners:
            banner_id = int(banner.get("id") or 0)
            if not banner_id:
                continue
            spend, shows, clicks, goals = banner_stats.get(banner_id, (0.0, 0, 0, 0.0))
            title, body = banner_copy(banner.get("textblocks"))
            if spend <= 0 and clicks <= 0 and goals <= 0:
                continue
            cabinet.texts.append(
                VkText(
                    banner_id=banner_id,
                    title=title,
                    body=body,
                    status=_clean(banner.get("status")),
                    spend=cost_with_vat(spend, vat_rate),
                    shows=shows,
                    clicks=clicks,
                    goals=goals,
                )
            )
        for segment in api.get_paged(token, "remarketing/segments", limit=50):
            cabinet.audiences.append(
                VkAudience(
                    name=_clean(segment.get("name") or "Сегмент"),
                    kind="сегмент",
                    users=int(_num(segment.get("users_count") or segment.get("entries_count"))),
                    created=_clean(str(segment.get("created") or "")[:10]),
                    status="" if str(segment.get("pass_condition") or "").isdigit() else _clean(segment.get("pass_condition")),
                )
            )
    except VkAdsError as exc:
        cabinet.error = str(exc)[:200]
        logger.warning("VK кабинет %s: %s", cabinet.name, exc)
    except Exception as exc:
        cabinet.error = str(exc)[:200]
        logger.warning("VK кабинет %s: %s", cabinet.name, exc)
    with _JOBS_LOCK:
        job.done += 1
    remember_partial(KIND, key, cabinet.client_id, cabinet)
    return cabinet


def _stats_by_id(api: VkAdsClient, token: str, section: str, period: dict) -> dict[int, tuple[float, int, int, float]]:
    payload = api.get(token, section, period)
    found = {}
    for item in payload.get("items") or []:
        if isinstance(item, dict) and item.get("id"):
            found[int(item["id"])] = metrics_of(item)
    return found


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "—")


def _block_text(block: object) -> str:
    if isinstance(block, dict):
        return _clean(block.get("text") or "")
    return _clean(block)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _num(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return 0.0
