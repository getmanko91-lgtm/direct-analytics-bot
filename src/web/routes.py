from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from src.auth import hash_password, verify_password
from src.config import Settings
from src.db.database import get_db
from src.db.models import Client, User
from src.services.analytics_export import build_analytics_xlsx, export_filename
from src.services.analytics_table import (
    fetch_analytics_table_cached,
    find_conversion_drought_clients,
    find_weekly_budget_overruns,
    format_analytics_telegram,
)
from src.services.client_balances import fetch_client_balances, format_balance
from src.services.client_analytics_bundle import fetch_client_analytics_bundle_cached
from src.services.budget_pacing import build_budget_pacing
from src.services.cpa_style import (
    cpa_chart_color,
    cpa_compare_display,
    cpa_highlight_class,
    format_period_short,
    previous_period,
)
from src.services.appmetrica_sync import sync_client_appmetrica_goals
from src.services.client_reports import fetch_client_reports_cached, format_period, metrics_to_display
from src.services.client_reports_export import build_client_reports_xlsx, export_filename as client_reports_export_filename
from src.services.kpi_table import fetch_kpi_table_cached
from src.services.app_settings import get_setting, set_setting
from src.services.message_delivery import ReportDeliveryError, deliver_report_message, effective_report_channel
from src.services.report_runner import run_all_reports, run_client_report
from src.max_notifier import MaxError
from src.telegram_notifier import TelegramError
from src.web.dependencies import TEMPLATES_DIR, get_app_settings, get_current_user
from src.web.urls import redirect_url, require_ascii_login

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


def _period_from_request(request: Request) -> tuple[date, date]:
    today = date.today()
    yesterday = today - timedelta(days=1)
    date_from = _parse_date(request.query_params.get("date_from"), yesterday)
    date_to = _parse_date(request.query_params.get("date_to"), yesterday)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _placement_filters_from_request(request: Request) -> tuple[int, float]:
    try:
        min_clicks = int(request.query_params.get("min_clicks", "1"))
    except ValueError:
        min_clicks = 1
    try:
        min_spend = float(request.query_params.get("min_spend", "0") or "0")
    except ValueError:
        min_spend = 0.0
    return max(min_clicks, 0), max(min_spend, 0.0)


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _report_send_label(db: Session, settings: Settings) -> str:
    channel = effective_report_channel(db, settings)
    return {
        "telegram": "Telegram",
        "max": "MAX",
        "both": "Telegram + MAX",
    }.get(channel, "мессенджер")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Неверный логин или пароль"},
            status_code=401,
        )
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def analytics_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _period_from_request(request)
    prev_from, prev_to = previous_period(date_from, date_to)
    today = date.today()
    yesterday = today - timedelta(days=1)

    rows = fetch_analytics_table_cached(db, settings, date_from, date_to)
    prev_rows = fetch_analytics_table_cached(db, settings, prev_from, prev_to)
    prev_cpa_by_goal = {
        (r.client_id, r.goal_id): r.cpa
        for r in prev_rows
        if not r.error and r.cpa is not None
    }

    display_rows = []
    for r in rows:
        cpa_compare, cpa_compare_class, cpa_compare_title = cpa_compare_display(
            r.cpa,
            prev_cpa_by_goal.get((r.client_id, r.goal_id)),
            fmt_money=_fmt_money,
        )
        display_rows.append(
        {
            "client_id": r.client_id,
            "client_name": r.client_name,
            "monthly_budget": _fmt_money(r.monthly_budget) if r.monthly_budget > 0 else "—",
            "weekly_budget": _fmt_money(r.weekly_budget) if r.weekly_budget > 0 else "—",
            "spend": _fmt_money(r.spend) if not r.error else "—",
            "impressions": _fmt_int(r.impressions) if not r.error else "—",
            "clicks": _fmt_int(r.clicks) if not r.error else "—",
            "cpc": _fmt_money(r.cpc) if r.cpc is not None else ("—" if not r.error else "—"),
            "goal_name": r.goal_name,
            "conversions": (
                str(int(r.conversions))
                if r.conversions == int(r.conversions)
                else f"{r.conversions:.2f}".replace(".", ",")
            )
            if not r.error
            else "—",
            "cpa": _fmt_money(r.cpa) if r.cpa is not None else ("—" if not r.error else r.error),
            "cpa_class": cpa_highlight_class(r.cpa) if not r.error else "",
            "cpa_compare": cpa_compare,
            "cpa_compare_class": cpa_compare_class,
            "cpa_compare_title": cpa_compare_title,
            "error": r.error,
            "show_client_block": r.show_client_block,
            "balance_amount": format_balance(r.balance.amount) if r.balance and r.balance.amount is not None else None,
            "balance_low": bool(r.balance and r.balance.is_low),
            "balance_error": r.balance.error if r.balance else None,
        }
        )

    return templates.TemplateResponse(
        request,
        "analytics.html",
        {
            "user": user,
            "rows": display_rows,
            "date_from": date_from,
            "date_to": date_to,
            "vat_percent": int(settings.vat_rate * 100),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "preset_yesterday": f"date_from={yesterday.isoformat()}&date_to={yesterday.isoformat()}",
            "preset_7days": f"date_from={(today - timedelta(days=7)).isoformat()}&date_to={yesterday.isoformat()}",
            "preset_month": f"date_from={today.replace(day=1).isoformat()}&date_to={yesterday.isoformat()}",
            "compare_period": format_period_short(prev_from, prev_to),
            "report_send_label": _report_send_label(db, settings),
        },
    )


@router.get("/analytics/export")
def analytics_export(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _period_from_request(request)
    rows = fetch_analytics_table_cached(db, settings, date_from, date_to)
    cpa_classes = [cpa_highlight_class(r.cpa) if not r.error else "" for r in rows]
    content = build_analytics_xlsx(
        rows,
        date_from,
        date_to,
        cpa_classes=cpa_classes,
        vat_percent=int(settings.vat_rate * 100),
    )
    filename = export_filename(date_from, date_to)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/kpi", response_class=HTMLResponse)
def kpi_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _period_from_request(request)
    today = date.today()
    yesterday = today - timedelta(days=1)

    rows = fetch_kpi_table_cached(db, settings, date_from, date_to)
    total_spend = sum(r.spend for r in rows if not r.error)
    total_conversions = sum(r.conversions for r in rows if not r.error)
    total_cpa = (total_spend / total_conversions) if total_conversions > 0 else None

    display_rows = [
        {
            "client_id": r.client_id,
            "client_name": r.client_name,
            "directologist": r.directologist,
            "spend": _fmt_money(r.spend) if not r.error else "—",
            "conversions": (
                str(int(r.conversions))
                if r.conversions == int(r.conversions)
                else f"{r.conversions:.2f}".replace(".", ",")
            )
            if not r.error
            else "—",
            "cpa": _fmt_money(r.cpa) if r.cpa is not None else ("—" if not r.error else "—"),
            "error": r.error,
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "kpi.html",
        {
            "user": user,
            "rows": display_rows,
            "summary": {
                "spend": _fmt_money(total_spend),
                "conversions": (
                    str(int(total_conversions))
                    if total_conversions == int(total_conversions)
                    else f"{total_conversions:.2f}".replace(".", ",")
                ),
                "cpa": _fmt_money(total_cpa) if total_cpa is not None else "—",
                "cpa_class": cpa_highlight_class(total_cpa),
            },
            "date_from": date_from,
            "date_to": date_to,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "preset_yesterday": f"date_from={yesterday.isoformat()}&date_to={yesterday.isoformat()}",
            "preset_7days": f"date_from={(today - timedelta(days=7)).isoformat()}&date_to={yesterday.isoformat()}",
            "preset_month": f"date_from={today.replace(day=1).isoformat()}&date_to={yesterday.isoformat()}",
        },
    )


def _client_reports_period(request: Request) -> tuple[date, date]:
    today = date.today()
    yesterday = today - timedelta(days=1)
    default_from = today.replace(day=1)
    date_from = _parse_date(request.query_params.get("date_from"), default_from)
    date_to = _parse_date(request.query_params.get("date_to"), yesterday)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    return date_from, date_to


def _client_report_to_display(report, vat_rate: float) -> dict:
    week_rows = []
    for (week_from, week_to), metrics in zip(report.weeks, report.week_metrics, strict=True):
        row = metrics_to_display(metrics, vat_rate)
        row["period"] = format_period(week_from, week_to)
        week_rows.append(row)

    total = metrics_to_display(report.total, vat_rate)
    if report.weeks:
        total_label = f"Итого {format_period(report.weeks[0][0], report.weeks[-1][1])}"
    else:
        total_label = "Итого"

    plan_budget = _fmt_money(report.plan_budget) if report.plan_budget > 0 else "—"

    return {
        "client_name": report.client_name,
        "error": report.error,
        "week_rows": week_rows,
        "total": total,
        "total_label": total_label,
        "plan_budget": plan_budget,
    }


@router.get("/client-reports", response_class=HTMLResponse)
def client_reports_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _client_reports_period(request)
    today = date.today()
    yesterday = today - timedelta(days=1)
    first_of_month = today.replace(day=1)
    prev_month_end = first_of_month - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)

    reports_raw = fetch_client_reports_cached(db, settings, date_from, date_to)
    reports = [_client_report_to_display(r, settings.vat_rate) for r in reports_raw]

    return templates.TemplateResponse(
        request,
        "client_reports.html",
        {
            "user": user,
            "reports": reports,
            "date_from": date_from,
            "date_to": date_to,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "preset_month": f"date_from={first_of_month.isoformat()}&date_to={yesterday.isoformat()}",
            "preset_prev_month": f"date_from={prev_month_start.isoformat()}&date_to={prev_month_end.isoformat()}",
        },
    )


@router.get("/client-reports/export")
def client_reports_export(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _client_reports_period(request)
    reports = fetch_client_reports_cached(db, settings, date_from, date_to)
    content = build_client_reports_xlsx(
        reports,
        date_from,
        date_to,
        vat_percent=int(settings.vat_rate * 100),
    )
    filename = client_reports_export_filename(date_from, date_to)
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/analytics/send")
def analytics_send_report(
    request: Request,
    page_date_from: str = Form(""),
    page_date_to: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    yesterday = date.today() - timedelta(days=1)
    view_from = _parse_date(page_date_from, yesterday)
    view_to = _parse_date(page_date_to, view_from)

    try:
        rows = fetch_analytics_table_cached(db, settings, yesterday, yesterday)
        drought = find_conversion_drought_clients(db, settings, yesterday)
        week_from, week_to, week_overruns = find_weekly_budget_overruns(db, settings, yesterday)
        message = format_analytics_telegram(
            rows,
            yesterday,
            yesterday,
            conversion_drought_clients=drought,
            weekly_budget_alerts=week_overruns,
            weekly_budget_period=(week_from, week_to),
        )
        target = deliver_report_message(db, settings, message)
        return RedirectResponse(
            redirect_url(
                "/",
                date_from=view_from.isoformat(),
                date_to=view_to.isoformat(),
                message=f"Сводка за вчера отправлена в {target}",
            ),
            status_code=303,
        )
    except (ReportDeliveryError, TelegramError, MaxError, Exception) as exc:
        return RedirectResponse(
            redirect_url(
                "/",
                date_from=view_from.isoformat(),
                date_to=view_to.isoformat(),
                error=str(exc)[:400],
            ),
            status_code=303,
        )


@router.get("/clients", response_class=HTMLResponse)
def clients_list(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    clients = (
        db.query(Client)
        .options(joinedload(Client.goals))
        .order_by(Client.name)
        .all()
    )
    balances = fetch_client_balances(settings.yandex_token, [c.yandex_login for c in clients])
    client_rows = []
    for client in clients:
        balance = balances.get(client.yandex_login)
        client_rows.append(
            {
                "client": client,
                "balance_amount": format_balance(balance.amount) if balance and balance.amount is not None else None,
                "balance_low": bool(balance and balance.is_low),
                "balance_error": balance.error if balance else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "clients/list.html",
        {
            "user": user,
            "client_rows": client_rows,
            "message": request.query_params.get("message"),
        },
    )


@router.get("/clients/{client_id}/analytics", response_class=HTMLResponse)
def client_analytics_page(
    request: Request,
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    date_from, date_to = _period_from_request(request)
    min_clicks, min_spend = _placement_filters_from_request(request)
    today = date.today()
    yesterday = today - timedelta(days=1)

    bundle = fetch_client_analytics_bundle_cached(
        db,
        settings,
        client_id,
        date_from,
        date_to,
        min_clicks=min_clicks,
        min_spend=min_spend,
    )
    if not bundle:
        return RedirectResponse("/clients", status_code=303)

    report = bundle.report
    placements_report = bundle.placements

    def _fmt_conv(value: float) -> str:
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}".replace(".", ",")

    by_cpa = sorted(
        [c for c in report.campaigns if c.cpa is not None and c.conversions > 0],
        key=lambda c: c.cpa,
        reverse=True,
    )

    display_campaigns = [
        {
            "campaign_name": c.campaign_name,
            "spend": _fmt_money(c.spend),
            "impressions": _fmt_int(c.impressions),
            "clicks": _fmt_int(c.clicks),
            "cpc": _fmt_money(c.cpc) if c.cpc is not None else "—",
            "conversions": _fmt_conv(c.conversions),
            "cpa": _fmt_money(c.cpa) if c.cpa is not None else "—",
            "cpa_class": c.cpa_class,
        }
        for c in report.campaigns
    ]

    month_budget = float(report.client.monthly_budget or 0)
    pacing = build_budget_pacing(month_budget, report.total_spend, date_from, date_to)

    def _fmt_deviation(value: float | None) -> str:
        if value is None:
            return "—"
        sign = "+" if value > 0 else ""
        return f"{sign}{value:.1f}".replace(".", ",") + "%"

    pacing_status_labels = {
        "ok": "В норме (±10%)",
        "over": "Перерасход",
        "under": "Недорасход",
        "none": "Бюджет не задан",
    }

    display_placements = []
    placement_names: list[str] = []
    placements_error = None
    wasted_spend = "—"
    if placements_report:
        placements_error = placements_report.error
        wasted_spend = _fmt_money(placements_report.total_wasted_spend)
        for row in placements_report.placements:
            display_placements.append(
                {
                    "placement": row.placement,
                    "spend": _fmt_money(row.spend),
                    "impressions": _fmt_int(row.impressions),
                    "clicks": _fmt_int(row.clicks),
                }
            )
            placement_names.append(row.placement)

    filter_query = (
        f"date_from={date_from.isoformat()}&date_to={date_to.isoformat()}"
        f"&min_clicks={min_clicks}&min_spend={min_spend}"
    )

    return templates.TemplateResponse(
        request,
        "clients/analytics.html",
        {
            "user": user,
            "client": report.client,
            "date_from": date_from,
            "date_to": date_to,
            "error": report.error,
            "total_spend": _fmt_money(report.total_spend) if not report.error else "—",
            "total_clicks": _fmt_int(report.total_clicks) if not report.error else "—",
            "total_conversions": _fmt_conv(report.total_conversions) if not report.error else "—",
            "monthly_budget": _fmt_money(month_budget) if month_budget > 0 else "—",
            "weekly_budget": _fmt_money(report.weekly_budget) if report.weekly_budget > 0 else "—",
            "daily_budget": _fmt_money(pacing.daily_budget) if pacing.has_budget else "—",
            "period_days": pacing.period_days,
            "expected_spend": _fmt_money(pacing.expected_spend) if pacing.has_budget else "—",
            "deviation_percent": _fmt_deviation(pacing.deviation_percent),
            "pacing_status": pacing.status,
            "pacing_status_label": pacing_status_labels[pacing.status],
            "campaigns": display_campaigns,
            "chart_labels": [c.campaign_name for c in by_cpa],
            "chart_values": [round(c.cpa, 2) for c in by_cpa if c.cpa is not None],
            "chart_colors": [cpa_chart_color(c.cpa) for c in by_cpa],
            "chart_height": min(420, max(220, len(by_cpa) * 42)),
            "vat_percent": int(settings.vat_rate * 100),
            "preset_yesterday": f"date_from={yesterday.isoformat()}&date_to={yesterday.isoformat()}",
            "preset_7days": f"date_from={(today - timedelta(days=7)).isoformat()}&date_to={yesterday.isoformat()}",
            "preset_month": f"date_from={today.replace(day=1).isoformat()}&date_to={yesterday.isoformat()}",
            "placements": display_placements,
            "placement_names_text": "\n".join(placement_names),
            "placements_count": len(display_placements),
            "placements_wasted_spend": wasted_spend,
            "placements_error": placements_error,
            "min_clicks": min_clicks,
            "min_spend": min_spend,
            "filter_query": filter_query,
        },
    )


@router.get("/clients/new", response_class=HTMLResponse)
def client_new_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "clients/form.html",
        {"user": user, "client": None, "error": None},
    )


@router.post("/clients/new")
def client_create(
    request: Request,
    name: str = Form(...),
    yandex_login: str = Form(...),
    metrika_counter_id: str = Form(""),
    appmetrica_application_id: str = Form(""),
    appmetrica_tracking_id: str = Form(""),
    telegram_chat_id: str = Form(""),
    max_chat_id: str = Form(""),
    spend_alert_threshold: float = Form(0),
    monthly_budget: float = Form(0),
    directologist: str = Form("Ксюша"),
    attribution_model: str = Form("AUTO"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    counter_id = int(metrika_counter_id) if metrika_counter_id.strip() else None
    appmetrica_id = int(appmetrica_application_id) if appmetrica_application_id.strip() else None
    try:
        yandex_login = require_ascii_login(yandex_login)
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "clients/form.html",
            {"user": user, "client": None, "error": str(exc)},
            status_code=400,
        )
    client = Client(
        name=name.strip(),
        yandex_login=yandex_login.strip(),
        metrika_counter_id=counter_id,
        appmetrica_application_id=appmetrica_id,
        appmetrica_tracking_id=appmetrica_tracking_id.strip(),
        telegram_chat_id=telegram_chat_id.strip(),
        max_chat_id=max_chat_id.strip(),
        spend_alert_threshold=spend_alert_threshold,
        monthly_budget=max(monthly_budget, 0),
        directologist=directologist if directologist in {"Ксюша", "Лариса"} else "Ксюша",
        attribution_model=attribution_model,
    )
    db.add(client)
    try:
        db.commit()
    except Exception:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "clients/form.html",
            {"user": user, "client": None, "error": "Клиент с таким логином уже существует"},
            status_code=400,
        )
    return RedirectResponse(f"/clients/{client.id}/goals", status_code=303)


@router.get("/clients/{client_id}/edit", response_class=HTMLResponse)
def client_edit_page(
    request: Request,
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    return templates.TemplateResponse(
        request,
        "clients/form.html",
        {"user": user, "client": client, "error": None},
    )


@router.post("/clients/{client_id}/edit")
def client_update(
    request: Request,
    client_id: int,
    name: str = Form(...),
    yandex_login: str = Form(...),
    metrika_counter_id: str = Form(""),
    appmetrica_application_id: str = Form(""),
    appmetrica_tracking_id: str = Form(""),
    telegram_chat_id: str = Form(""),
    max_chat_id: str = Form(""),
    spend_alert_threshold: float = Form(0),
    monthly_budget: float = Form(0),
    directologist: str = Form("Ксюша"),
    attribution_model: str = Form("AUTO"),
    is_active: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)

    try:
        client.yandex_login = require_ascii_login(yandex_login)
    except ValueError as exc:
        client.name = name.strip()
        return templates.TemplateResponse(
            request,
            "clients/form.html",
            {"user": user, "client": client, "error": str(exc)},
            status_code=400,
        )

    client.name = name.strip()
    client.metrika_counter_id = int(metrika_counter_id) if metrika_counter_id.strip() else None
    client.appmetrica_application_id = int(appmetrica_application_id) if appmetrica_application_id.strip() else None
    client.appmetrica_tracking_id = appmetrica_tracking_id.strip()
    client.telegram_chat_id = telegram_chat_id.strip()
    client.max_chat_id = max_chat_id.strip()
    client.spend_alert_threshold = spend_alert_threshold
    client.monthly_budget = max(monthly_budget, 0)
    client.directologist = directologist if directologist in {"Ксюша", "Лариса"} else "Ксюша"
    client.attribution_model = attribution_model
    client.is_active = is_active == "on"
    db.commit()
    return RedirectResponse("/clients", status_code=303)


@router.post("/clients/{client_id}/delete")
def client_delete(
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.get(Client, client_id)
    if client:
        db.delete(client)
        db.commit()
    return RedirectResponse(redirect_url("/clients", message="Клиент удалён"), status_code=303)


@router.get("/clients/{client_id}/goals", response_class=HTMLResponse)
def client_goals_page(
    request: Request,
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.query(Client).options(joinedload(Client.goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    selected_count = len(selected_goal_ids(client))
    return templates.TemplateResponse(
        request,
        "clients/goals.html",
        {
            "user": user,
            "client": client,
            "selected_count": selected_count,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "warn": request.query_params.get("warn"),
        },
    )


@router.post("/clients/{client_id}/goals/sync")
def client_goals_sync(
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    client = db.query(Client).options(joinedload(Client.goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    try:
        result = sync_client_goals(
            db,
            client,
            settings.yandex_token,
            metrika_token=settings.yandex_metrika_token,
        )
        message = f"Загружено целей: {len(result.goals)}"
        if result.sources:
            message += f". Источник: {', '.join(result.sources)}."
        query = {"message": message[:300]}
        if result.warnings:
            query["warn"] = " ".join(result.warnings)[:600]
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/goals", **query),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/goals", error=str(exc)[:400]),
            status_code=303,
        )


@router.post("/clients/{client_id}/goals")
def client_goals_save(
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    selected: list[str] = Form(default=[]),
):
    client = db.query(Client).options(joinedload(Client.goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)

    selected_ids = {int(value) for value in selected}
    for goal in client.goals:
        goal.is_selected = goal.goal_id in selected_ids
    db.commit()
    return RedirectResponse(
        redirect_url(f"/clients/{client_id}/goals", message="Выбор целей сохранён"),
        status_code=303,
    )


@router.get("/clients/{client_id}/appmetrica-goals", response_class=HTMLResponse)
def client_appmetrica_goals_page(
    request: Request,
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.query(Client).options(joinedload(Client.appmetrica_goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    return templates.TemplateResponse(
        request,
        "clients/appmetrica_goals.html",
        {
            "user": user,
            "client": client,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "warn": request.query_params.get("warn"),
        },
    )


@router.post("/clients/{client_id}/appmetrica-goals/sync")
def client_appmetrica_goals_sync(
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    client = db.query(Client).options(joinedload(Client.appmetrica_goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    try:
        result = sync_client_appmetrica_goals(db, client, settings)
        message = f"Загружено событий: {len(result.goals)}"
        query = {"message": message}
        if result.warnings:
            query["warn"] = " ".join(result.warnings)[:600]
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/appmetrica-goals", **query),
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/appmetrica-goals", error=str(exc)[:400]),
            status_code=303,
        )


@router.post("/clients/{client_id}/appmetrica-goals")
def client_appmetrica_goals_save(
    client_id: int,
    install_goal: str = Form(""),
    purchase_goal: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    client = db.query(Client).options(joinedload(Client.appmetrica_goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)

    valid_keys = {goal.event_key for goal in client.appmetrica_goals}
    install_key = install_goal.strip() if install_goal.strip() in valid_keys else ""
    purchase_key = purchase_goal.strip() if purchase_goal.strip() in valid_keys else ""
    if install_key and purchase_key and install_key == purchase_key:
        return RedirectResponse(
            redirect_url(
                f"/clients/{client_id}/appmetrica-goals",
                error="Цель установки и покупки не могут совпадать",
            ),
            status_code=303,
        )

    for goal in client.appmetrica_goals:
        goal.role = ""
    for goal in client.appmetrica_goals:
        if goal.event_key == install_key:
            goal.role = "install"
        elif goal.event_key == purchase_key:
            goal.role = "purchase"
    db.commit()
    return RedirectResponse(
        redirect_url(f"/clients/{client_id}/appmetrica-goals", message="Цели AppMetrica сохранены"),
        status_code=303,
    )


@router.get("/clients/{client_id}/preview", response_class=HTMLResponse)
def client_preview(
    request: Request,
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    client = db.query(Client).options(joinedload(Client.goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)

    preview_text = None
    error = None
    try:
        preview_text = run_client_report(db, settings, client)
    except Exception as exc:
        error = str(exc)

    return templates.TemplateResponse(
        request,
        "clients/preview.html",
        {
            "user": user,
            "client": client,
            "preview_text": preview_text,
            "error": error,
            "report_send_label": _report_send_label(db, settings),
        },
    )


@router.post("/clients/{client_id}/send")
def client_send_now(
    client_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    client = db.query(Client).options(joinedload(Client.goals)).get(client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)

    try:
        message = run_client_report(db, settings, client)
        target = deliver_report_message(db, settings, message, client=client)
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/preview", message=f"Отправлено в {target}"),
            status_code=303,
        )
    except (ReportDeliveryError, TelegramError, MaxError, Exception) as exc:
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/preview", error=str(exc)[:400]),
            status_code=303,
        )


@router.post("/reports/run-all")
def reports_run_all(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    run_all_reports(db, settings)
    return RedirectResponse(redirect_url("/", message="Сводка за вчера отправлена"), status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    settings = get_settings_dep(request)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "telegram_chat_id": get_setting(db, "telegram_chat_id") or settings.telegram_chat_id,
            "max_chat_id": get_setting(db, "max_chat_id") or settings.max_chat_id,
            "max_bot_token_configured": bool(get_setting(db, "max_bot_token") or settings.max_bot_token),
            "appmetrica_token_configured": bool(
                get_setting(db, "appmetrica_token") or settings.yandex_appmetrica_token
            ),
            "report_channel": effective_report_channel(db, settings),
            "report_time": get_setting(db, "report_time") or settings.report_time,
            "timezone": get_setting(db, "timezone") or settings.timezone,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/settings")
def settings_save(
    request: Request,
    telegram_chat_id: str = Form(""),
    max_chat_id: str = Form(""),
    max_bot_token: str = Form(""),
    appmetrica_token: str = Form(""),
    report_channel: str = Form("telegram"),
    report_time: str = Form("09:00"),
    timezone: str = Form("Europe/Moscow"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    channel = report_channel.strip().lower()
    if channel not in {"telegram", "max", "both"}:
        channel = "telegram"
    set_setting(db, "telegram_chat_id", telegram_chat_id.strip())
    set_setting(db, "max_chat_id", max_chat_id.strip())
    if max_bot_token.strip():
        set_setting(db, "max_bot_token", max_bot_token.strip())
    if appmetrica_token.strip():
        set_setting(db, "appmetrica_token", appmetrica_token.strip())
    set_setting(db, "report_channel", channel)
    set_setting(db, "report_time", report_time.strip())
    set_setting(db, "timezone", timezone.strip())
    from src.web.app import reschedule_daily_reports

    reschedule_daily_reports(get_settings_dep(request))
    return RedirectResponse(redirect_url("/settings", message="Настройки сохранены"), status_code=303)


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request, user: User = Depends(get_current_user)):
    return templates.TemplateResponse(
        request,
        "profile.html",
        {"user": user, "error": None, "message": request.query_params.get("message")},
    )


@router.post("/profile")
def profile_update(
    request: Request,
    display_name: str = Form(""),
    current_password: str = Form(...),
    new_password: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "profile.html",
            {"user": user, "error": "Текущий пароль неверный", "message": None},
            status_code=400,
        )

    user.display_name = display_name.strip()
    if new_password.strip():
        user.password_hash = hash_password(new_password.strip())
    db.commit()
    return RedirectResponse(redirect_url("/profile", message="Профиль обновлён"), status_code=303)
