from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from src.config import Settings
from src.services.kpi_table import is_kpi_campaign
from src.services.parallel_fetch import map_parallel
from src.services.report_store import begin_collection, remember_partial, save_finished
from src.services.rsya_network import CabinetRef, cabinet_refs
from src.vat import cost_with_vat
from src.yandex_direct import YandexDirectClient, _parse_float, _parse_int, conversions_for_goal

logger = logging.getLogger(__name__)

RESULT_TTL_SECONDS = 6 * 60 * 60
BEST_CPA_MIN_CONVERSIONS = 3.0
MISSING_TEXT = "Текст недоступен"


def extract_ad_copy(ad: dict) -> tuple[str, str]:
    """Заголовок и текст объявления. Пустая пара — текста в ответе Директа нет."""
    text_ad = ad.get("TextAd") if isinstance(ad.get("TextAd"), dict) else {}
    title = _join_parts(text_ad.get("Title"), text_ad.get("Title2"))
    body = _clean(text_ad.get("Text"))
    if title or body:
        return title, body

    mobile = ad.get("MobileAppAd") if isinstance(ad.get("MobileAppAd"), dict) else {}
    title = _clean(mobile.get("Title"))
    body = _clean(mobile.get("Text"))
    if title or body:
        return title, body

    responsive = ad.get("ResponsiveAd") if isinstance(ad.get("ResponsiveAd"), dict) else {}
    titles = _pieces(responsive.get("Titles"), "Title")
    texts = _pieces(responsive.get("Texts"), "Text")
    if titles or texts:
        return " | ".join(titles), " | ".join(texts)

    for shopping_key in ("ShoppingAd", "ListingAd"):
        shopping = ad.get(shopping_key) if isinstance(ad.get(shopping_key), dict) else {}
        defaults = _pieces(shopping.get("DefaultTexts"), "Text")
        if defaults:
            return "", " | ".join(defaults)

    dynamic = ad.get("DynamicTextAd") if isinstance(ad.get("DynamicTextAd"), dict) else {}
    body = _clean(dynamic.get("Text"))
    if body:
        return "", body
    return "", ""


def direct_ad_url(login: str, campaign_id: int, ad_id: int) -> str:
    """Ссылка в кабинет Директа. Работает и для удалённого объявления: открывается по его номеру."""
    login = (login or "").strip()
    if not login or not ad_id:
        return ""
    query = {"ulogin": login, "banners-ids": str(ad_id)}
    if campaign_id:
        query["campaigns-ids"] = str(campaign_id)
    return "https://direct.yandex.ru/dna/grid/banners?" + urlencode(query)


def text_identity(title: str, body: str, ad_id: int) -> str:
    compact = " ".join(f"{title} {body}".casefold().split())
    return compact or f"#{ad_id}"


def parse_cabinet_ads(
    rows: list[dict[str, str]],
    goal_ids: list[int] | tuple[int, ...],
    attribution_model: str,
    vat_rate: float,
) -> dict[int, "AdStat"]:
    totals: dict[int, list[float]] = {}
    for row in rows:
        if not is_kpi_campaign(row.get("CampaignName") or ""):
            continue
        ad_id = _parse_int(row.get("AdId"))
        if not ad_id:
            continue
        bucket = totals.setdefault(ad_id, [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += _parse_float(row.get("Cost", "0"))
        bucket[1] += _parse_int(row.get("Clicks", "0"))
        bucket[2] += _parse_int(row.get("Impressions", "0"))
        bucket[3] += sum(conversions_for_goal(row, gid, attribution_model) for gid in goal_ids)
        campaign_id = _parse_int(row.get("CampaignId"))
        if campaign_id:
            bucket[4] = float(campaign_id)
    ads: dict[int, AdStat] = {}
    for ad_id, (cost_raw, clicks, impressions, conversions, campaign_id) in totals.items():
        if cost_raw <= 0 and clicks <= 0 and conversions <= 0:
            continue
        ads[ad_id] = AdStat(
            ad_id=ad_id,
            title="",
            body="",
            spend=cost_with_vat(cost_raw, vat_rate),
            clicks=int(clicks),
            impressions=int(impressions),
            conversions=conversions,
            campaign_id=int(campaign_id),
        )
    return ads


def apply_ad_copies(ads: dict[int, "AdStat"], raw_ads: list[dict]) -> None:
    _remember_copies(ads, raw_ads)


def _remember_copies(ads: dict[int, "AdStat"], raw_ads: list[dict]) -> set[int]:
    returned: set[int] = set()
    for raw in raw_ads:
        raw_id = raw.get("Id")
        ad_id = raw_id if isinstance(raw_id, int) else _parse_int(str(raw_id or ""))
        if not ad_id:
            continue
        returned.add(ad_id)
        stat = ads.get(ad_id)
        if stat is None:
            continue
        if str(raw.get("State") or "") == "ARCHIVED":
            stat.archived = True
        title, body = extract_ad_copy(raw)
        if title or body:
            stat.title, stat.body = title, body
    return returned


@dataclass
class AdStat:
    ad_id: int
    title: str
    body: str
    spend: float
    clicks: int
    impressions: int
    conversions: float
    campaign_id: int = 0
    deleted: bool = False
    archived: bool = False


@dataclass(frozen=True)
class TextShare:
    client_name: str
    directologist: str
    spend: float
    clicks: int
    conversions: float
    href: str = ""

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions > 0 else None

    @property
    def cpc(self) -> float | None:
        return self.spend / self.clicks if self.clicks > 0 else None


@dataclass(frozen=True)
class RankedText:
    title: str
    body: str
    spend: float
    clicks: int
    impressions: int
    conversions: float
    cabinets: tuple[TextShare, ...]
    archived: bool = False
    href: str = ""

    @property
    def cpa(self) -> float | None:
        return self.spend / self.conversions if self.conversions > 0 else None

    @property
    def cpc(self) -> float | None:
        return self.spend / self.clicks if self.clicks > 0 else None

    @property
    def label(self) -> str:
        if self.title and self.body:
            return f"{self.title} — {self.body}"
        return self.title or self.body or MISSING_TEXT


@dataclass
class CabinetAds:
    cabinet: CabinetRef
    ads: list[AdStat] = field(default_factory=list)
    error: str | None = None
    text_error: str | None = None


@dataclass(frozen=True)
class AdTextsReport:
    date_from: date
    date_to: date
    texts: tuple[RankedText, ...]
    errors: tuple[tuple[str, str], ...]
    cabinets_total: int

    @property
    def total_spend(self) -> float:
        return sum(item.spend for item in self.texts)

    @property
    def total_conversions(self) -> float:
        return sum(item.conversions for item in self.texts)

    @property
    def texts_with_sales(self) -> int:
        return sum(1 for item in self.texts if item.conversions > 0)


def build_ad_texts_report(
    cabinets: list[CabinetAds],
    date_from: date,
    date_to: date,
) -> AdTextsReport:
    buckets: dict[str, dict] = {}
    errors: list[tuple[str, str]] = []
    for item in cabinets:
        if item.error:
            errors.append((item.cabinet.client_name, item.error))
            continue
        if item.text_error:
            errors.append((item.cabinet.client_name, item.text_error))
        for ad in item.ads:
            key = text_identity(ad.title, ad.body, ad.ad_id)
            bucket = buckets.setdefault(
                key,
                {"title": ad.title, "body": ad.body, "deleted": ad.deleted, "archived": ad.archived, "shares": {}},
            )
            if not bucket["title"] and not bucket["body"] and (ad.title or ad.body):
                bucket["title"] = ad.title
                bucket["body"] = ad.body
            if not ad.archived:
                bucket["archived"] = False
            share = bucket["shares"].setdefault(
                item.cabinet.client_id,
                {
                    "client_name": item.cabinet.client_name,
                    "directologist": item.cabinet.directologist,
                    "spend": 0.0,
                    "clicks": 0,
                    "conversions": 0.0,
                    "href": "",
                    "href_conversions": -1.0,
                },
            )
            share["spend"] += ad.spend
            share["clicks"] += ad.clicks
            share["conversions"] += ad.conversions
            if ad.ad_id and ad.conversions >= share["href_conversions"]:
                share["href"] = direct_ad_url(item.cabinet.yandex_login, ad.campaign_id, ad.ad_id)
                share["href_conversions"] = ad.conversions
            bucket["impressions"] = bucket.get("impressions", 0) + ad.impressions

    ranked: list[RankedText] = []
    for bucket in buckets.values():
        shares = tuple(
            TextShare(
                client_name=raw["client_name"],
                directologist=raw["directologist"],
                spend=raw["spend"],
                clicks=raw["clicks"],
                conversions=raw["conversions"],
                href=raw["href"],
            )
            for raw in bucket["shares"].values()
        )
        shares = tuple(sorted(shares, key=lambda share: (-share.conversions, -share.spend)))
        if bucket["title"] or bucket["body"]:
            title = bucket["title"]
        elif bucket["deleted"]:
            title = "Объявление удалено"
        else:
            title = MISSING_TEXT
        ranked.append(
            RankedText(
                title=title,
                body=bucket["body"],
                spend=sum(share.spend for share in shares),
                clicks=sum(share.clicks for share in shares),
                impressions=bucket.get("impressions", 0),
                conversions=sum(share.conversions for share in shares),
                cabinets=shares,
                archived=bool(bucket["archived"]),
                href=shares[0].href if shares else "",
            )
        )
    ranked.sort(key=lambda item: (-item.conversions, -item.spend))
    return AdTextsReport(
        date_from=date_from,
        date_to=date_to,
        texts=tuple(ranked),
        errors=tuple(errors),
        cabinets_total=len(cabinets),
    )


def best_text_labels(report: AdTextsReport) -> tuple[RankedText | None, RankedText | None]:
    with_sales = [item for item in report.texts if item.conversions > 0]
    best_conversions = with_sales[0] if with_sales else None
    eligible = [item for item in report.texts if item.conversions >= BEST_CPA_MIN_CONVERSIONS and item.cpa is not None]
    best_cpa = min(eligible, key=lambda item: item.cpa or 0) if eligible else None
    return best_conversions, best_cpa


def format_ad_texts_csv(report: AdTextsReport) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Заголовок", "Текст", "Конверсии", "CPA, ₽", "Расход, ₽", "Клики", "Проекты"])
    for item in report.texts:
        projects = "; ".join(
            f"{share.client_name}: {share.conversions:g} конв., {share.spend:.2f} ₽"
            for share in item.cabinets
        )
        writer.writerow(
            [
                item.title,
                item.body,
                f"{item.conversions:g}",
                "" if item.cpa is None else f"{item.cpa:.2f}",
                f"{item.spend:.2f}",
                item.clicks,
                projects,
            ]
        )
    return buffer.getvalue()


def _load_cabinet(settings: Settings, ref: CabinetRef, date_from: date, date_to: date) -> CabinetAds:
    try:
        api = YandexDirectClient(settings.yandex_token, ref.yandex_login)
        rows = api.fetch_ad_performance_report(date_from, date_to, list(ref.goal_ids), ref.attribution_model)
        ads = parse_cabinet_ads(rows, ref.goal_ids, ref.attribution_model, settings.vat_rate)
    except Exception as exc:
        logger.warning("Тексты: ошибка кабинета %s: %s", ref.client_name, exc)
        return CabinetAds(cabinet=ref, error=str(exc)[:300])

    text_error = None
    if ads:
        try:
            raw_ads, copy_error = api.fetch_ads(list(ads))
            returned = _remember_copies(ads, raw_ads)
            missing_campaigns = [
                ad.campaign_id
                for ad in ads.values()
                if ad.ad_id not in returned and ad.campaign_id
            ]
            if missing_campaigns:
                try:
                    archived_ads, archived_error = api.fetch_archived_ads(missing_campaigns)
                    returned |= _remember_copies(ads, archived_ads)
                    if archived_error:
                        copy_error = "; ".join(part for part in (copy_error, archived_error) if part)
                except Exception as exc:
                    logger.warning("Архивные тексты кабинета %s: %s", ref.client_name, exc)
                    copy_error = "; ".join(part for part in (copy_error, str(exc)[:200]) if part)
            for ad in ads.values():
                if ad.title or ad.body:
                    continue
                if ad.ad_id not in returned:
                    ad.deleted = True
            if copy_error and not any(ad.title or ad.body for ad in ads.values()):
                text_error = copy_error
        except Exception as exc:
            logger.warning("Тексты объявлений кабинета %s: %s", ref.client_name, exc)
            text_error = str(exc)[:300]
    return CabinetAds(cabinet=ref, ads=list(ads.values()), text_error=text_error)


@dataclass
class AdTextsJob:
    total: int
    started_at: float = field(default_factory=time.time)
    done: int = 0
    finished_at: float | None = None
    cabinets: list[CabinetAds] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def expired(self) -> bool:
        return self.finished_at is not None and time.time() - self.finished_at > RESULT_TTL_SECONDS


_JOBS: dict[tuple, AdTextsJob] = {}
_JOBS_LOCK = threading.Lock()


def _job_key(settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> tuple:
    return (
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        "texts-v2",
        tuple((ref.client_id, ref.goal_ids, ref.attribution_model) for ref in refs),
    )


def _run_job(
    job: AdTextsJob,
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
    key: tuple,
) -> None:
    def _one(ref: CabinetRef) -> CabinetAds:
        result = _load_cabinet(settings, ref, date_from, date_to)
        with _JOBS_LOCK:
            job.done += 1
            job.cabinets.append(result)
        remember_partial("ad-texts", key, ref.client_id, result)
        return result

    try:
        if refs:
            map_parallel(_one, refs)
    except Exception:
        logger.exception("Тексты: сбор прервался, уже готовые кабинеты сохранены")
        with _JOBS_LOCK:
            if _JOBS.get(key) is job:
                _JOBS.pop(key, None)
        return
    job.finished_at = time.time()
    save_finished("ad-texts", key, job.cabinets, job.finished_at)


def get_ad_texts_job(
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
    *,
    force: bool = False,
) -> AdTextsJob:
    key = _job_key(settings, refs, date_from, date_to)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.running:
            return job
        if job and not force and not job.expired:
            return job
        finished, partial = begin_collection("ad-texts", key, force=force, ttl_seconds=RESULT_TTL_SECONDS)
        if finished is not None:
            finished_at, cabinets = finished
            job = AdTextsJob(total=len(cabinets), done=len(cabinets), finished_at=finished_at, cabinets=cabinets)
            _JOBS[key] = job
            return job
        job = AdTextsJob(total=len(refs), done=len(partial), cabinets=list(partial.values()))
        _JOBS[key] = job
    pending = [ref for ref in refs if ref.client_id not in partial]
    threading.Thread(
        target=_run_job,
        args=(job, settings, pending, date_from, date_to, key),
        name="ad-texts",
        daemon=True,
    ).start()
    return job


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _join_parts(*parts: object) -> str:
    return " — ".join(piece for piece in (_clean(part) for part in parts) if piece)


def _pieces(value: object, key: str) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        text = _clean(value)
        return [text] if text else []
    if not isinstance(value, list):
        return []
    found: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = _clean(item)
        elif isinstance(item, dict):
            text = _clean(item.get(key) or item.get("Text") or item.get("Title"))
        else:
            text = ""
        if text:
            found.append(text)
    return found


__all__ = [
    "MISSING_TEXT",
    "AdTextsReport",
    "apply_ad_copies",
    "best_text_labels",
    "build_ad_texts_report",
    "cabinet_refs",
    "extract_ad_copy",
    "format_ad_texts_csv",
    "get_ad_texts_job",
    "parse_cabinet_ads",
    "text_identity",
]
