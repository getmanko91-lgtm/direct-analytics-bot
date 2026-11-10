from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from src.config import Settings
from src.db.models import User
from src.services.cpa_style import cpa_highlight_class
from src.services.vk_analytics import (
    CAMPAIGN_KIND_LABELS,
    CAMPAIGN_KINDS,
    DESTINATION_LABELS,
    VkCabinet,
    VkJob,
    build_vk_report,
    cabinet_type_split,
    project_name,
    campaign_kind,
    campaign_type_totals,
    get_vk_job,
    status_label,
)
from src.web.dependencies import get_current_user
from src.web.routes import RSYA_PRESETS, _fmt_int, _fmt_money, _rsya_period, get_settings_dep, templates

router = APIRouter()

SECTIONS = {
    "": "summary",
    "campaigns": "campaigns",
    "texts": "texts",
    "audiences": "audiences",
    "clients": "clients",
}


def _fmt_goals(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return _fmt_int(int(round(value)))
    return f"{value:.1f}".replace(".", ",")


def _cpa(spend: float, goals: float) -> float | None:
    return spend / goals if goals > 0 else None


def _share_row(share: dict) -> dict:
    cpa = _cpa(share["spend"], share["goals"])
    return {
        "name": share["name"],
        "goals": _fmt_goals(share["goals"]),
        "goals_value": share["goals"],
        "spend": _fmt_money(share["spend"]),
        "cpa": "—" if cpa is None else _fmt_money(cpa),
    }


@router.get("/vk", response_class=HTMLResponse)
@router.get("/vk/{section}", response_class=HTMLResponse)
def vk_page(
    request: Request,
    section: str = "",
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings_dep),
):
    view = SECTIONS.get(section)
    if view is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404)
    date_from, date_to, preset = _rsya_period(request.query_params)
    force = request.query_params.get("refresh") == "1"
    job = get_vk_job(settings, date_from, date_to, force=force)
    query = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat()}
    if preset:
        query = {"period": preset}
    context = {
        "user": user,
        "section": view,
        "date_from": date_from,
        "date_to": date_to,
        "preset": preset,
        "presets": RSYA_PRESETS,
        "query": query,
        "running": job.running,
        "progress_done": job.done,
        "progress_total": job.total,
        "error": job.error,
        "report": None,
    }
    if not job.running:
        context["report"] = _present(job)
    return templates.TemplateResponse(request, "vk.html", context)


def _present(job: VkJob) -> dict:
    report = build_vk_report(job.cabinets)
    best = None
    eligible = [item for item in report["cabinets"] if item.goals >= 3]
    if eligible:
        best = min(eligible, key=lambda item: item.spend / item.goals)
    return {
        "clients_with_sales": report["with_sales"],
        "clients_total": len(report["cabinets"]),
        "total_goals": _fmt_goals(report["total_goals"]),
        "total_spend": _fmt_money(report["total_spend"]),
        "total_clicks": _fmt_int(report["total_clicks"]),
        "best_cpa": None
        if best is None
        else {"name": project_name(best.name), "cpa": _fmt_money(best.spend / best.goals), "goals": _fmt_goals(best.goals)},
        "errors": [(item.name, item.error) for item in report["cabinets"] if item.error],
        "types": _type_rows(campaign_type_totals(report["campaigns"])),
        "cabinets": [_cabinet_row(item) for item in report["cabinets"]],
        "campaigns": [_campaign_row(cabinet, plan) for cabinet, plan in report["campaigns"]],
        "texts": [_text_row(item) for item in report["texts"]],
        "audiences": [_audience_row(cabinet, item) for cabinet, item in report["audiences"]],
    }


def _type_rows(totals: dict[str, dict]) -> list[dict]:
    rows = []
    for kind in CAMPAIGN_KINDS:
        item = totals[kind]
        senler = kind == "senler"
        cpa = None if senler else _cpa(item["spend"], item["goals"])
        rows.append(
            {
                "key": kind,
                "label": CAMPAIGN_KIND_LABELS[kind],
                "campaigns": _fmt_int(item["campaigns"]),
                "goals": "в Senler" if senler else _fmt_goals(item["goals"]),
                "spend": _fmt_money(item["spend"]),
                "cpa": "—" if cpa is None else _fmt_money(cpa),
                "cpa_class": cpa_highlight_class(cpa),
            }
        )
    return rows


def _cabinet_row(item: VkCabinet) -> dict:
    split = cabinet_type_split(item)
    order_spend = 0.0 if split is None else split["zk"]["spend"]
    join_spend = 0.0 if split is None else split["join"]["spend"]
    subscriptions = 0.0 if split is None else split["join"]["goals"]
    price = _cpa(join_spend, subscriptions)
    name = project_name(item.name)
    return {
        "name": name,
        "order_spend": "—" if split is None else _fmt_money(order_spend),
        "join_spend": "—" if split is None else _fmt_money(join_spend),
        "subscriptions": "—" if split is None else _fmt_goals(subscriptions),
        "price": "—" if price is None else _fmt_money(price),
        "cpa_class": cpa_highlight_class(price),
        "goals_value": item.goals,
        "spend": _fmt_money(item.spend),
        "search": f"{name} {item.name}".casefold(),
    }


def _campaign_row(cabinet: VkCabinet, plan) -> dict:
    kind = campaign_kind(plan.name)
    senler = kind == "senler"
    cpa = None if senler else _cpa(plan.spend, plan.goals)
    return {
        "name": plan.name,
        "client": cabinet.name,
        "kind": kind,
        "kind_label": CAMPAIGN_KIND_LABELS[kind],
        "objective": plan.objective or "—",
        "status": status_label(plan.status),
        "goals": "в Senler" if senler else _fmt_goals(plan.goals),
        "goals_value": 0 if senler else plan.goals,
        "spend": _fmt_money(plan.spend),
        "clicks": _fmt_int(plan.clicks),
        "cpa": "—" if cpa is None else _fmt_money(cpa),
        "cpa_class": cpa_highlight_class(cpa),
        "search": f"{plan.name} {cabinet.name} {plan.objective}".casefold(),
    }


def _text_row(item: dict) -> dict:
    cpa = _cpa(item["spend"], item["goals"])
    kind = item.get("kind") or "other"
    label = DESTINATION_LABELS.get(kind, "Другое")
    return {
        "title": item["title"],
        "body": item["body"],
        "kind": kind,
        "kind_label": label,
        "deleted": item["deleted"],
        "goals": _fmt_goals(item["goals"]),
        "goals_value": item["goals"],
        "spend": _fmt_money(item["spend"]),
        "clicks": _fmt_int(item["clicks"]),
        "cpa": "—" if cpa is None else _fmt_money(cpa),
        "cpa_class": cpa_highlight_class(cpa),
        "search": f"{item['title']} {item['body']} {label}".casefold(),
        "shares": [_share_row(share) for share in item["shares"]],
    }


def _audience_row(cabinet: VkCabinet, item) -> dict:
    return {
        "name": item.name,
        "client": cabinet.name,
        "kind": item.kind,
        "users": _fmt_int(item.users) if item.users else "—",
        "users_value": item.users,
        "created": item.created or "—",
        "search": f"{item.name} {cabinet.name} {item.kind}".casefold(),
    }
