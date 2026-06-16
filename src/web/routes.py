from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from src.auth import hash_password, verify_password
from src.config import Settings
from src.db.database import get_db
from src.db.models import Client, User
from src.services.analytics_table import fetch_analytics_table, format_analytics_telegram
from src.services.client_balances import fetch_client_balances, format_balance
from src.services.client_campaigns import fetch_client_campaign_report
from src.services.cpa_style import cpa_highlight_class
from src.services.goals_sync import selected_goal_ids, sync_client_goals
from src.services.report_runner import get_setting, run_all_reports, run_client_report, set_setting
from src.telegram_notifier import TelegramError, TelegramNotifier
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


def _fmt_money(value: float) -> str:
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


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
    today = date.today()
    yesterday = today - timedelta(days=1)

    rows = fetch_analytics_table(db, settings, date_from, date_to)
    display_rows = [
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
            "error": r.error,
            "show_client_block": r.show_client_block,
            "balance_amount": format_balance(r.balance.amount) if r.balance and r.balance.amount is not None else None,
            "balance_low": bool(r.balance and r.balance.is_low),
            "balance_error": r.balance.error if r.balance else None,
        }
        for r in rows
    ]

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
        },
    )


@router.post("/analytics/send")
def analytics_send_telegram(
    request: Request,
    date_from: str = Form(...),
    date_to: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_app_settings),
):
    d_from = _parse_date(date_from, date.today() - timedelta(days=1))
    d_to = _parse_date(date_to, d_from)
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    chat_id = get_setting(db, "telegram_chat_id") or settings.telegram_chat_id
    try:
        rows = fetch_analytics_table(db, settings, d_from, d_to)
        message = format_analytics_telegram(rows, d_from, d_to)
        TelegramNotifier(settings.telegram_bot_token, chat_id, proxy=settings.telegram_proxy).send_message(message)
        return RedirectResponse(
            redirect_url("/", date_from=d_from.isoformat(), date_to=d_to.isoformat(), message="Сводка отправлена в Telegram"),
            status_code=303,
        )
    except (TelegramError, Exception) as exc:
        return RedirectResponse(
            redirect_url("/", date_from=d_from.isoformat(), date_to=d_to.isoformat(), error=str(exc)[:400]),
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
    today = date.today()
    yesterday = today - timedelta(days=1)

    report = fetch_client_campaign_report(db, settings, client_id, date_from, date_to)
    if not report:
        return RedirectResponse("/clients", status_code=303)

    def _fmt_conv(value: float) -> str:
        if value == int(value):
            return str(int(value))
        return f"{value:.2f}".replace(".", ",")

    by_conversions = sorted(report.campaigns, key=lambda c: c.conversions, reverse=True)
    chart_rows = [c for c in by_conversions if c.conversions > 0]

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
            "campaigns": display_campaigns,
            "chart_labels": [c.campaign_name for c in chart_rows],
            "chart_values": [c.conversions for c in chart_rows],
            "chart_height": min(420, max(220, len(chart_rows) * 42)),
            "vat_percent": int(settings.vat_rate * 100),
            "preset_yesterday": f"date_from={yesterday.isoformat()}&date_to={yesterday.isoformat()}",
            "preset_7days": f"date_from={(today - timedelta(days=7)).isoformat()}&date_to={yesterday.isoformat()}",
            "preset_month": f"date_from={today.replace(day=1).isoformat()}&date_to={yesterday.isoformat()}",
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
    telegram_chat_id: str = Form(""),
    spend_alert_threshold: float = Form(0),
    monthly_budget: float = Form(0),
    attribution_model: str = Form("AUTO"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    counter_id = int(metrika_counter_id) if metrika_counter_id.strip() else None
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
        telegram_chat_id=telegram_chat_id.strip(),
        spend_alert_threshold=spend_alert_threshold,
        monthly_budget=max(monthly_budget, 0),
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
    telegram_chat_id: str = Form(""),
    spend_alert_threshold: float = Form(0),
    monthly_budget: float = Form(0),
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
    client.telegram_chat_id = telegram_chat_id.strip()
    client.spend_alert_threshold = spend_alert_threshold
    client.monthly_budget = max(monthly_budget, 0)
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
        if result.warnings:
            message += (
                ". Дополнительно из Директа не загрузилось (не критично, если список целей ниже есть). "
                f"{' '.join(result.warnings)}"
            )
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/goals", message=message[:400]),
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
        {"user": user, "client": client, "preview_text": preview_text, "error": error},
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

    chat_id = client.telegram_chat_id or get_setting(db, "telegram_chat_id") or settings.telegram_chat_id
    try:
        message = run_client_report(db, settings, client)
        TelegramNotifier(settings.telegram_bot_token, chat_id, proxy=settings.telegram_proxy).send_message(message)
        return RedirectResponse(
            redirect_url(f"/clients/{client_id}/preview", message="Отправлено"),
            status_code=303,
        )
    except (TelegramError, Exception) as exc:
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
    return RedirectResponse(redirect_url("/", message="Отчёты отправлены"), status_code=303)


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
            "report_time": get_setting(db, "report_time") or settings.report_time,
            "timezone": get_setting(db, "timezone") or settings.timezone,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/settings")
def settings_save(
    request: Request,
    telegram_chat_id: str = Form(""),
    report_time: str = Form("09:00"),
    timezone: str = Form("Europe/Moscow"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    set_setting(db, "telegram_chat_id", telegram_chat_id.strip())
    set_setting(db, "report_time", report_time.strip())
    set_setting(db, "timezone", timezone.strip())
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
