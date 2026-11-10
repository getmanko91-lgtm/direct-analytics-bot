from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from src.config import Settings
from src.services.kpi_table import is_kpi_campaign
from src.services.parallel_fetch import map_parallel
from src.services.rsya_network import (
    CabinetPlacements,
    CabinetRef,
    PlacementStat,
    RsyaNetworkReport,
    _fmt_conv,
    _fmt_cost,
    _fmt_money,
    build_network_report,
    cabinet_refs,
    format_all_placements_csv,
)
from src.vat import cost_with_vat
from src.yandex_direct import YandexDirectClient, _parse_float, _parse_int, conversions_for_goal

logger = logging.getLogger(__name__)

RESULT_TTL_SECONDS = 6 * 60 * 60


def parse_cabinet_keywords(
    rows: list[dict[str, str]],
    goal_ids: list[int] | tuple[int, ...],
    attribution_model: str,
    vat_rate: float,
) -> dict[str, PlacementStat]:
    totals: dict[str, list[float]] = {}
    labels: dict[str, str] = {}
    for row in rows:
        if not is_kpi_campaign(row.get("CampaignName") or ""):
            continue
        raw = " ".join((row.get("Criterion") or "").split())
        if not raw or raw == "—" or raw.startswith("---"):
            continue
        key = raw.casefold()
        labels.setdefault(key, raw)
        bucket = totals.setdefault(key, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += _parse_float(row.get("Cost", "0"))
        bucket[1] += _parse_int(row.get("Clicks", "0"))
        bucket[2] += _parse_int(row.get("Impressions", "0"))
        bucket[3] += sum(conversions_for_goal(row, gid, attribution_model) for gid in goal_ids)
    return {
        labels[key]: PlacementStat(
            spend=cost_with_vat(cost_raw, vat_rate),
            clicks=int(clicks),
            impressions=int(impressions),
            conversions=conversions,
        )
        for key, (cost_raw, clicks, impressions, conversions) in totals.items()
    }


def format_keywords_csv(report: RsyaNetworkReport) -> str:
    return format_all_placements_csv(report, item_header="Ключевик")


def format_keywords_telegram(report: RsyaNetworkReport, *, limit: int = 25, good_limit: int = 10) -> str:
    period = report.date_from.strftime("%d.%m.%Y")
    if report.date_from != report.date_to:
        period += f" — {report.date_to.strftime('%d.%m.%Y')}"
    lines = [
        "Операторам",
        "",
        f"Ключевики без конверсий во всех кабинетах (от {_fmt_money(report.min_spend)} ₽)",
        f"Поиск и РСЯ, {period}",
        "",
        f"Слито: {_fmt_money(report.total_dead_spend)} ₽",
    ]
    share = report.dead_share_percent
    if share is not None:
        lines[-1] += f" ({share:.0f}% расхода на ключевики)"
    lines.append(f"Ключевиков: {len(report.dead)}")
    lines.append("")
    for item in report.dead[:limit]:
        cabinets = ", ".join(share.client_name for share in item.cabinets[:3])
        if len(item.cabinets) > 3:
            cabinets += f" и ещё {len(item.cabinets) - 3}"
        lines.append(
            f"{item.placement} — {_fmt_money(item.spend)} ₽, {item.clicks} кл., "
            f"CPC {_fmt_cost(item.cpc)} ₽, 0 конв. ({cabinets})"
        )
    if len(report.dead) > limit:
        lines.append("")
        lines.append(f"Ещё {len(report.dead) - limit} ключевиков — на странице «Ключевики».")
    if not report.dead:
        lines.append("Ключевиков без конверсий выше порога нет.")

    if report.good:
        lines.extend(
            [
                "",
                f"Удачные ключевики (от {_fmt_conv(report.good_min_conversions)} конв., "
                f"CPA не выше среднего {_fmt_cost(report.avg_cpa)} ₽)",
                "",
            ]
        )
        for item in report.good[:good_limit]:
            lines.append(
                f"{item.placement} — CPA {_fmt_cost(item.cpa)} ₽, {_fmt_conv(item.conversions)} конв., "
                f"расход {_fmt_money(item.spend)} ₽, CPC {_fmt_cost(item.cpc)} ₽"
            )
    return "\n".join(lines)


def _load_cabinet(settings: Settings, ref: CabinetRef, date_from: date, date_to: date) -> CabinetPlacements:
    try:
        api = YandexDirectClient(settings.yandex_token, ref.yandex_login)
        rows = api.fetch_keyword_report(date_from, date_to, list(ref.goal_ids), ref.attribution_model)
        keywords = parse_cabinet_keywords(rows, ref.goal_ids, ref.attribution_model, settings.vat_rate)
        return CabinetPlacements(cabinet=ref, placements=keywords)
    except Exception as exc:
        logger.warning("Ключевики: ошибка кабинета %s: %s", ref.client_name, exc)
        return CabinetPlacements(cabinet=ref, placements={}, error=str(exc)[:300])


@dataclass
class KeywordJob:
    total: int
    started_at: float = field(default_factory=time.time)
    done: int = 0
    finished_at: float | None = None
    cabinets: list[CabinetPlacements] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.finished_at is None

    @property
    def expired(self) -> bool:
        return self.finished_at is not None and time.time() - self.finished_at > RESULT_TTL_SECONDS


_JOBS: dict[tuple, KeywordJob] = {}
_JOBS_LOCK = threading.Lock()


def _job_key(settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> tuple:
    return (
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        tuple((ref.client_id, ref.goal_ids, ref.attribution_model) for ref in refs),
    )


def _run_job(job: KeywordJob, settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> None:
    def _one(ref: CabinetRef) -> CabinetPlacements:
        result = _load_cabinet(settings, ref, date_from, date_to)
        with _JOBS_LOCK:
            job.done += 1
        return result

    try:
        job.cabinets = map_parallel(_one, refs)
    finally:
        job.finished_at = time.time()


def get_keyword_job(
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
    *,
    force: bool = False,
) -> KeywordJob:
    key = _job_key(settings, refs, date_from, date_to)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.running:
            return job
        if job and not force and not job.expired:
            return job
        job = KeywordJob(total=len(refs))
        _JOBS[key] = job
    threading.Thread(
        target=_run_job,
        args=(job, settings, refs, date_from, date_to),
        name="keywords-network",
        daemon=True,
    ).start()
    return job


def build_keyword_report(
    cabinets: list[CabinetPlacements],
    date_from: date,
    date_to: date,
    *,
    min_spend: float,
    good_min_conversions: float,
) -> RsyaNetworkReport:
    return build_network_report(
        cabinets,
        date_from,
        date_to,
        min_spend=min_spend,
        good_min_conversions=good_min_conversions,
    )


__all__ = [
    "build_keyword_report",
    "cabinet_refs",
    "format_keywords_csv",
    "format_keywords_telegram",
    "get_keyword_job",
    "parse_cabinet_keywords",
]
