from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response


class LoginRequiredMiddleware(BaseHTTPMiddleware):
    """Все страницы, кроме /login и /static, только для авторизованных."""

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        if path.startswith("/static"):
            return await call_next(request)

        if path == "/login":
            return await call_next(request)

        if not request.session.get("user_id"):
            return RedirectResponse(url="/login", status_code=303)

        return await call_next(request)
