"""Клиент VK Рекламы: агентский токен и токен кабинета, как в сервисе target."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import requests

from src.db.database import DATA_DIR

_TOKEN_FILE = DATA_DIR / "vk_tokens.json"
_LOCK = threading.Lock()


class VkAdsError(RuntimeError):
    pass


class VkAdsClient:
    def __init__(self, client_id: str, client_secret: str, agency_id: str) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.agency_id = agency_id.strip()
        self._memory: dict[str, dict] = {}

    def agency_token(self) -> str:
        return self._token(f"agency:{self.agency_id}", agency_client_id=None)

    def client_token(self, agency_client_id: int) -> str:
        return self._token(f"client:{agency_client_id}", agency_client_id=agency_client_id)

    def get(self, token: str, section: str, params: dict | None = None, *, version: int = 2) -> dict:
        url = f"https://ads.vk.com/api/v{version}/{section}.json"
        response = None
        payload = {}
        for attempt in range(5):
            response = requests.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
                timeout=45,
            )
            payload = _json_body(response)
            if response.status_code != 429:
                break
            time.sleep(1.5 * (attempt + 1))
        if response.status_code >= 400:
            code = ""
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    code = str(error.get("code") or error.get("message") or "")
                else:
                    code = str(error or payload.get("error_description") or "")
            raise VkAdsError(f"VK {section}: {response.status_code} {code}"[:300])
        if not isinstance(payload, dict):
            raise VkAdsError(f"VK {section}: пустой ответ")
        return payload

    def get_paged(self, token: str, section: str, params: dict | None = None, *, limit: int = 50) -> list[dict]:
        items: list[dict] = []
        offset = 0
        query = dict(params or {})
        while True:
            page = self.get(token, section, {**query, "limit": limit, "offset": offset})
            batch = page.get("items") or page.get("objects") or []
            if not isinstance(batch, list):
                break
            items.extend(item for item in batch if isinstance(item, dict))
            count = page.get("count")
            offset += int(page.get("limit") or limit)
            if not batch or (isinstance(count, int) and offset >= count) or len(batch) < limit:
                break
            if offset > 5000:
                break
        return items

    def _token(self, key: str, *, agency_client_id: int | None) -> str:
        with _LOCK:
            stored = self._memory.get(key) or _read_store().get(key)
            if stored and float(stored.get("expires_at") or 0) > time.time() + 120:
                self._memory[key] = stored
                return str(stored["access_token"])
            refresh = str((stored or {}).get("refresh_token") or "")
        if refresh:
            try:
                credentials = self._oauth({"grant_type": "refresh_token", "refresh_token": refresh})
                return self._save_token(key, credentials)
            except VkAdsError:
                pass
        body = {
            "grant_type": "agency_client_credentials" if agency_client_id else "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if agency_client_id:
            body["agency_client_id"] = str(agency_client_id)
        return self._save_token(key, self._oauth(body))

    def _save_token(self, key: str, credentials: dict) -> str:
        access = str(credentials.get("access_token") or "")
        if not access:
            raise VkAdsError("VK не вернул access_token")
        expires_in = int(credentials.get("expires_in") or 3600)
        record = {
            "access_token": access,
            "refresh_token": str(credentials.get("refresh_token") or ""),
            "expires_at": time.time() + max(expires_in - 3600, 300),
        }
        with _LOCK:
            self._memory[key] = record
            store = _read_store()
            store[key] = record
            _TOKEN_FILE.write_text(json.dumps(store), encoding="utf-8")
        return access

    def _oauth(self, body: dict) -> dict:
        response = requests.post(
            "https://ads.vk.com/api/v2/oauth2/token.json",
            data=body,
            timeout=30,
        )
        payload = _json_body(response)
        if response.status_code >= 400 or not isinstance(payload, dict) or not payload.get("access_token"):
            error = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or payload.get("error_description") or "")
            raise VkAdsError(f"VK oauth: {response.status_code} {error}"[:300])
        return payload


def _json_body(response: requests.Response):
    try:
        return response.json()
    except ValueError:
        return {}


def _read_store() -> dict:
    if not _TOKEN_FILE.exists():
        return {}
    try:
        payload = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
