from __future__ import annotations

import logging
import re

import requests

logger = logging.getLogger(__name__)

MAX_API_URL = "https://platform-api.max.ru/messages"
MAX_MESSAGE_LENGTH = 4000


class MaxError(RuntimeError):
    pass


class MaxNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token.strip()
        self._chat_id = self._normalize_id(chat_id)

    @staticmethod
    def _normalize_id(value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            return ""
        if cleaned.lstrip("-").isdigit():
            return str(int(cleaned))
        return cleaned

    def send_message(self, text: str) -> None:
        if not self._bot_token:
            raise MaxError("Не задан MAX_BOT_TOKEN в .env")
        if not self._chat_id:
            raise MaxError(
                "Не указан MAX chat_id. Задайте MAX_CHAT_ID в .env "
                "или chat_id для клиента в настройках."
            )

        chunks = _split_message(text, MAX_MESSAGE_LENGTH - 50)
        for chunk in chunks:
            try:
                self._post(chunk, format_mode="html")
            except MaxError as exc:
                if _is_html_format_error(exc):
                    logger.warning("MAX HTML failed, retrying as plain text")
                    self._post(_html_to_plain(chunk), format_mode=None)
                else:
                    raise

    def send_error(self, error: str) -> None:
        self.send_message(f"❌ <b>Ошибка direct-analytics-bot</b>\n\n{_escape_html(error)}")

    def _post(self, text: str, format_mode: str | None) -> None:
        params = {"chat_id": self._chat_id}
        payload: dict = {
            "text": text,
            "notify": True,
            "disable_link_preview": True,
        }
        if format_mode:
            payload["format"] = format_mode

        headers = {
            "Authorization": self._bot_token,
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                MAX_API_URL,
                params=params,
                json=payload,
                headers=headers,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise MaxError(f"Не удалось связаться с MAX: {exc}") from exc

        if response.status_code >= 400:
            hint = _hint_for_max_error(response)
            raise MaxError(f"MAX API ({response.status_code}): {hint}")

        logger.info("Сообщение отправлено в MAX (chat_id=%s)", self._chat_id)


def _split_message(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def _html_to_plain(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _is_html_format_error(exc: MaxError) -> bool:
    msg = str(exc).lower()
    return "format" in msg or "html" in msg or "parse" in msg


def _hint_for_max_error(response: requests.Response) -> str:
    try:
        body = response.json()
        message = body.get("message") or body.get("error") or body.get("description")
        if isinstance(message, dict):
            message = message.get("message") or str(message)
        if message:
            return str(message)
    except ValueError:
        pass
    text = response.text[:300]
    if response.status_code == 401:
        return "Неверный токен бота. Проверьте MAX_BOT_TOKEN."
    if response.status_code == 403:
        return "Нет доступа. Проверьте, что бот добавлен в чат."
    return text or "Неизвестная ошибка"
