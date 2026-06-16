from __future__ import annotations

from urllib.parse import urlencode


def redirect_url(path: str, **query: str) -> str:
    """Build a redirect path with UTF-8-safe query parameters."""
    if not query:
        return path
    return f"{path}?{urlencode(query, encoding='utf-8')}"


def require_ascii_login(login: str, field_name: str = "Логин кабинета") -> str:
    login = login.strip()
    if not login:
        raise ValueError(f"{field_name} не может быть пустым")
    try:
        login.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{field_name} должен быть латиницей (логин Яндекс.Директ), "
            f"без кириллицы. Сейчас: «{login}»"
        ) from exc
    return login
