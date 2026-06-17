from __future__ import annotations

from dataclasses import dataclass

from src.services.runtime_cache import get_or_set
from src.yandex_direct import YandexDirectClient, YandexDirectError

BALANCE_LOW_THRESHOLD = 10_000.0


@dataclass(frozen=True)
class ClientBalance:
    amount: float | None
    error: str | None = None

    @property
    def is_low(self) -> bool:
        return self.amount is not None and self.amount < BALANCE_LOW_THRESHOLD


def fetch_client_balances(token: str, logins: list[str]) -> dict[str, ClientBalance]:
    cleaned = sorted({login.strip() for login in logins if login and login.strip()})
    if not cleaned:
        return {}

    cache_key = ("balances", token, tuple(cleaned))

    def _load() -> dict[str, ClientBalance]:
        api = YandexDirectClient(token)
        try:
            amounts = api.fetch_account_balances(cleaned)
        except YandexDirectError as exc:
            message = str(exc)[:200]
            return {login: ClientBalance(amount=None, error=message) for login in cleaned}

        result: dict[str, ClientBalance] = {}
        for login in cleaned:
            if login in amounts:
                result[login] = ClientBalance(amount=amounts[login])
            else:
                result[login] = ClientBalance(amount=None, error="Баланс не найден в ответе API")
        return result

    return get_or_set(cache_key, _load, ttl_seconds=120)


def format_balance(amount: float) -> str:
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")
