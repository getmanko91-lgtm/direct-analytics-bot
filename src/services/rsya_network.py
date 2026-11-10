from __future__ import annotations

import csv
import io
import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy.orm import Session, joinedload

from src.config import Settings
from src.db.models import Client
from src.services.kpi_table import is_kpi_campaign
from src.services.parallel_fetch import map_parallel
from src.vat import cost_with_vat
from src.yandex_direct import YandexDirectClient, _parse_float, _parse_int, conversions_for_goal

logger = logging.getLogger(__name__)

RESULT_TTL_SECONDS = 6 * 60 * 60
DEFAULT_MIN_SPEND = 600.0
DEFAULT_GOOD_MIN_CONVERSIONS = 3.0


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


@dataclass(frozen=True)
class CabinetRef:
    client_id: int
    client_name: str
    directologist: str
    yandex_login: str
    goal_ids: tuple[int, ...]
    attribution_model: str


@dataclass(frozen=True)
class PlacementStat:
    spend: float
    clicks: int
    impressions: int
    conversions: float


@dataclass(frozen=True)
class CabinetPlacements:
    cabinet: CabinetRef
    placements: dict[str, PlacementStat]
    error: str | None = None


@dataclass(frozen=True)
class CabinetShare:
    client_id: int
    client_name: str
    directologist: str
    spend: float
    clicks: int
    conversions: float

    @property
    def cpa(self) -> float | None:
        return _ratio(self.spend, self.conversions)

    @property
    def cpc(self) -> float | None:
        return _ratio(self.spend, self.clicks)


@dataclass(frozen=True)
class NetworkPlacement:
    placement: str
    spend: float
    clicks: int
    impressions: int
    conversions: float
    cabinets: tuple[CabinetShare, ...]

    @property
    def cpa(self) -> float | None:
        return _ratio(self.spend, self.conversions)

    @property
    def cpc(self) -> float | None:
        return _ratio(self.spend, self.clicks)


@dataclass(frozen=True)
class PartialWaste:
    placement: str
    cabinet: CabinetShare
    network_conversions: float
    network_cpa: float | None


@dataclass(frozen=True)
class CabinetWaste:
    client_id: int
    client_name: str
    directologist: str
    rsya_spend: float
    wasted_spend: float
    wasted_count: int


@dataclass(frozen=True)
class RsyaNetworkReport:
    date_from: date
    date_to: date
    min_spend: float
    dead: tuple[NetworkPlacement, ...]
    partial: tuple[PartialWaste, ...]
    cabinets: tuple[CabinetWaste, ...]
    errors: tuple[tuple[str, str], ...]
    total_rsya_spend: float
    total_dead_spend: float
    cabinets_total: int
    good: tuple[NetworkPlacement, ...] = ()
    all_placements: tuple[NetworkPlacement, ...] = ()
    total_rsya_conversions: float = 0.0
    good_min_conversions: float = DEFAULT_GOOD_MIN_CONVERSIONS

    @property
    def avg_cpa(self) -> float | None:
        return _ratio(self.total_rsya_spend, self.total_rsya_conversions)

    @property
    def dead_share_percent(self) -> float | None:
        if self.total_rsya_spend <= 0:
            return None
        return self.total_dead_spend / self.total_rsya_spend * 100


def parse_cabinet_placements(
    rows: list[dict[str, str]],
    goal_ids: list[int] | tuple[int, ...],
    attribution_model: str,
    vat_rate: float,
) -> dict[str, PlacementStat]:
    totals: dict[str, list[float]] = {}
    for row in rows:
        placement = (row.get("Placement") or "").strip()
        if not placement or placement == "—":
            continue
        if not is_kpi_campaign(row.get("CampaignName") or ""):
            continue
        bucket = totals.setdefault(placement, [0.0, 0.0, 0.0, 0.0])
        bucket[0] += _parse_float(row.get("Cost", "0"))
        bucket[1] += _parse_int(row.get("Clicks", "0"))
        bucket[2] += _parse_int(row.get("Impressions", "0"))
        bucket[3] += sum(conversions_for_goal(row, gid, attribution_model) for gid in goal_ids)
    return {
        name: PlacementStat(
            spend=cost_with_vat(cost_raw, vat_rate),
            clicks=int(clicks),
            impressions=int(impressions),
            conversions=conversions,
        )
        for name, (cost_raw, clicks, impressions, conversions) in totals.items()
    }


def build_network_report(
    cabinets: list[CabinetPlacements],
    date_from: date,
    date_to: date,
    *,
    min_spend: float = DEFAULT_MIN_SPEND,
    good_min_conversions: float = DEFAULT_GOOD_MIN_CONVERSIONS,
) -> RsyaNetworkReport:
    by_placement: dict[str, list[tuple[CabinetRef, PlacementStat]]] = {}
    errors: list[tuple[str, str]] = []
    for item in cabinets:
        if item.error:
            errors.append((item.cabinet.client_name, item.error))
            continue
        for name, stat in item.placements.items():
            by_placement.setdefault(name, []).append((item.cabinet, stat))

    total_spend = sum(stat.spend for entries in by_placement.values() for _, stat in entries)
    total_conversions = sum(stat.conversions for entries in by_placement.values() for _, stat in entries)
    avg_cpa = _ratio(total_spend, total_conversions)

    dead: list[NetworkPlacement] = []
    good: list[NetworkPlacement] = []
    all_placements: list[NetworkPlacement] = []
    partial: list[PartialWaste] = []
    for name, entries in by_placement.items():
        shares = tuple(
            sorted(
                (
                    CabinetShare(
                        client_id=cabinet.client_id,
                        client_name=cabinet.client_name,
                        directologist=cabinet.directologist,
                        spend=stat.spend,
                        clicks=stat.clicks,
                        conversions=stat.conversions,
                    )
                    for cabinet, stat in entries
                ),
                key=lambda share: share.spend,
                reverse=True,
            )
        )
        spend = sum(share.spend for share in shares)
        conversions = sum(share.conversions for share in shares)
        placement = NetworkPlacement(
            placement=name,
            spend=spend,
            clicks=sum(stat.clicks for _, stat in entries),
            impressions=sum(stat.impressions for _, stat in entries),
            conversions=conversions,
            cabinets=shares,
        )
        all_placements.append(placement)
        if conversions <= 0:
            if spend >= min_spend:
                dead.append(placement)
            continue
        network_cpa = spend / conversions
        if conversions >= good_min_conversions and avg_cpa is not None and network_cpa <= avg_cpa:
            good.append(
                replace(
                    placement,
                    cabinets=tuple(sorted(shares, key=lambda share: share.conversions, reverse=True)),
                )
            )
        for share in shares:
            if share.conversions <= 0 and share.spend >= min_spend:
                partial.append(
                    PartialWaste(
                        placement=name,
                        cabinet=share,
                        network_conversions=conversions,
                        network_cpa=network_cpa,
                    )
                )

    dead.sort(key=lambda item: item.spend, reverse=True)
    all_placements.sort(key=lambda item: item.spend, reverse=True)
    good.sort(key=lambda item: (-item.conversions, item.cpa or 0))
    partial.sort(key=lambda item: item.cabinet.spend, reverse=True)

    dead_by_client: dict[int, list[float]] = {}
    for placement in dead:
        for share in placement.cabinets:
            bucket = dead_by_client.setdefault(share.client_id, [0.0, 0.0])
            bucket[0] += share.spend
            bucket[1] += 1

    cabinet_rows: list[CabinetWaste] = []
    for item in cabinets:
        if item.error:
            continue
        rsya_spend = sum(stat.spend for stat in item.placements.values())
        wasted, count = dead_by_client.get(item.cabinet.client_id, [0.0, 0.0])
        cabinet_rows.append(
            CabinetWaste(
                client_id=item.cabinet.client_id,
                client_name=item.cabinet.client_name,
                directologist=item.cabinet.directologist,
                rsya_spend=rsya_spend,
                wasted_spend=wasted,
                wasted_count=int(count),
            )
        )
    cabinet_rows.sort(key=lambda row: row.wasted_spend, reverse=True)

    total_rsya = sum(row.rsya_spend for row in cabinet_rows)
    return RsyaNetworkReport(
        date_from=date_from,
        date_to=date_to,
        min_spend=min_spend,
        dead=tuple(dead),
        partial=tuple(partial),
        cabinets=tuple(cabinet_rows),
        errors=tuple(errors),
        total_rsya_spend=total_rsya,
        total_dead_spend=sum(item.spend for item in dead),
        cabinets_total=len(cabinets),
        good=tuple(good),
        all_placements=tuple(all_placements),
        total_rsya_conversions=total_conversions,
        good_min_conversions=good_min_conversions,
    )


def cabinet_refs(db: Session) -> tuple[list[CabinetRef], list[str]]:
    clients = (
        db.query(Client)
        .options(joinedload(Client.goals))
        .filter(Client.is_active.is_(True))
        .order_by(Client.name)
        .all()
    )
    refs: list[CabinetRef] = []
    without_goals: list[str] = []
    for client in clients:
        goal_ids = tuple(sorted(goal.goal_id for goal in client.goals if goal.is_selected))
        if not goal_ids:
            without_goals.append(client.name)
            continue
        refs.append(
            CabinetRef(
                client_id=client.id,
                client_name=client.name,
                directologist=client.directologist or "—",
                yandex_login=client.yandex_login,
                goal_ids=goal_ids,
                attribution_model=client.attribution_model,
            )
        )
    return refs, without_goals


@dataclass
class NetworkJob:
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


_JOBS: dict[tuple, NetworkJob] = {}
_JOBS_LOCK = threading.Lock()


def _job_key(settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> tuple:
    return (
        settings.yandex_token,
        date_from.isoformat(),
        date_to.isoformat(),
        tuple((ref.client_id, ref.goal_ids, ref.attribution_model) for ref in refs),
    )


def _load_cabinet(settings: Settings, ref: CabinetRef, date_from: date, date_to: date) -> CabinetPlacements:
    try:
        api = YandexDirectClient(settings.yandex_token, ref.yandex_login)
        rows = api.fetch_rsya_placement_report(
            date_from,
            date_to,
            list(ref.goal_ids),
            ref.attribution_model,
            by_campaign=True,
        )
        placements = parse_cabinet_placements(rows, ref.goal_ids, ref.attribution_model, settings.vat_rate)
        return CabinetPlacements(cabinet=ref, placements=placements)
    except Exception as exc:
        logger.warning("РСЯ площадки: ошибка кабинета %s: %s", ref.client_name, exc)
        return CabinetPlacements(cabinet=ref, placements={}, error=str(exc)[:300])


def _run_job(job: NetworkJob, settings: Settings, refs: list[CabinetRef], date_from: date, date_to: date) -> None:
    def _one(ref: CabinetRef) -> CabinetPlacements:
        result = _load_cabinet(settings, ref, date_from, date_to)
        with _JOBS_LOCK:
            job.done += 1
        return result

    try:
        job.cabinets = map_parallel(_one, refs)
    finally:
        job.finished_at = time.time()


def get_network_job(
    settings: Settings,
    refs: list[CabinetRef],
    date_from: date,
    date_to: date,
    *,
    force: bool = False,
) -> NetworkJob:
    key = _job_key(settings, refs, date_from, date_to)
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job and job.running:
            return job
        if job and not force and not job.expired:
            return job
        job = NetworkJob(total=len(refs))
        _JOBS[key] = job
    threading.Thread(
        target=_run_job,
        args=(job, settings, refs, date_from, date_to),
        name="rsya-network",
        daemon=True,
    ).start()
    return job


def _fmt_money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _fmt_cost(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_conv(value: float) -> str:
    return f"{value:g}".replace(".", ",")


def format_all_placements_csv(report: RsyaNetworkReport) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Площадка",
            "Расход, ₽",
            "Клики",
            "CPC, ₽",
            "Конверсии",
            "CPA по всем кабинетам, ₽",
            "Кабинетов",
            "Проекты без конверсий",
            "Проекты с конверсиями",
        ]
    )
    for item in report.all_placements:
        zero = [share for share in item.cabinets if share.conversions <= 0]
        converting = sorted(
            (share for share in item.cabinets if share.conversions > 0),
            key=lambda share: share.conversions,
            reverse=True,
        )
        writer.writerow(
            [
                item.placement,
                _fmt_cost(item.spend),
                item.clicks,
                _fmt_cost(item.cpc),
                _fmt_conv(item.conversions),
                _fmt_cost(item.cpa),
                len(item.cabinets),
                ", ".join(f"{share.client_name} ({_fmt_cost(share.spend)} ₽)" for share in zero),
                ", ".join(
                    f"{share.client_name} ({_fmt_conv(share.conversions)} конв., CPA {_fmt_cost(share.cpa)} ₽)"
                    for share in converting
                ),
            ]
        )
    return buffer.getvalue()


def format_network_telegram(report: RsyaNetworkReport, *, limit: int = 25, good_limit: int = 10) -> str:
    period = report.date_from.strftime("%d.%m.%Y")
    if report.date_from != report.date_to:
        period += f" — {report.date_to.strftime('%d.%m.%Y')}"
    lines = [
        "Операторам",
        "",
        f"Площадки РСЯ без конверсий во всех кабинетах (от {_fmt_money(report.min_spend)} ₽)",
        period,
        "",
        f"Слито: {_fmt_money(report.total_dead_spend)} ₽",
    ]
    share = report.dead_share_percent
    if share is not None:
        lines[-1] += f" ({share:.0f}% расхода РСЯ)"
    lines.append(f"Площадок: {len(report.dead)}")
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
        lines.append(f"Ещё {len(report.dead) - limit} площадок — на странице «Площадки РСЯ».")
    if not report.dead:
        lines.append("Площадок без конверсий выше порога нет.")

    if report.good:
        lines.extend(
            [
                "",
                f"Удачные площадки (от {_fmt_conv(report.good_min_conversions)} конв., "
                f"CPA не выше среднего по РСЯ {_fmt_cost(report.avg_cpa)} ₽)",
                "",
            ]
        )
        for item in report.good[:good_limit]:
            lines.append(
                f"{item.placement} — CPA {_fmt_cost(item.cpa)} ₽, {_fmt_conv(item.conversions)} конв., "
                f"расход {_fmt_money(item.spend)} ₽, CPC {_fmt_cost(item.cpc)} ₽"
            )
    return "\n".join(lines)
