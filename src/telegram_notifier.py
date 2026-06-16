from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        proxy: str | None = None,
    ) -> None:
        self._bot_token = bot_token.strip()
        self._chat_id = self._normalize_chat_id(chat_id)
        self._proxy = (proxy or "").strip() or None

    @staticmethod
    def _normalize_chat_id(chat_id: str) -> str:
        cleaned = (chat_id or "").strip()
        if not cleaned:
            return ""
        if cleaned.lstrip("-").isdigit():
            return str(int(cleaned))
        return cleaned

    def send_message(self, text: str) -> None:
        if not self._chat_id:
            raise TelegramError(
                "Не указан Telegram chat_id. Задайте TELEGRAM_CHAT_ID в .env "
                "или chat_id для клиента в настройках."
            )

        chunks = _split_message(text, MAX_MESSAGE_LENGTH - 100)
        for chunk in chunks:
            try:
                self._post(chunk, parse_mode="HTML")
            except TelegramError as exc:
                if _is_html_parse_error(exc):
                    logger.warning("Telegram HTML failed, retrying as plain text")
                    self._post(chunk, parse_mode=None)
                else:
                    raise

    def send_error(self, error: str) -> None:
        self.send_message(f"❌ <b>Ошибка direct-analytics-bot</b>\n\n{_escape_html(error)}")

    def _request_kwargs(self) -> dict:
        kwargs: dict = {"timeout": 30}
        if self._proxy:
            kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
        return kwargs

    def _post(self, text: str, parse_mode: str | None) -> None:
        url = TELEGRAM_API_URL.format(token=self._bot_token)
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            response = requests.post(url, json=payload, **self._request_kwargs())
        except requests.RequestException as exc:
            raise TelegramError(_format_connection_error(exc, self._proxy)) from exc

        try:
            body = response.json()
        except ValueError:
            body = {"description": response.text}

        if response.status_code != 200 or not body.get("ok"):
            description = body.get("description", response.text)
            hint = _hint_for_telegram_error(description, self._chat_id)
            raise TelegramError(f"Telegram API ({response.status_code}): {description}. {hint}")

        logger.info("Сообщение отправлено в Telegram (chat_id=%s)", self._chat_id)


def _format_connection_error(exc: requests.RequestException, proxy: str | None) -> str:
    message = str(exc)
    if _is_network_unreachable(message):
        if proxy:
            return (
                "Не удалось связаться с Telegram через прокси. "
                f"Проверьте TELEGRAM_PROXY в .env (сейчас задан). Ошибка: {message}"
            )
        return (
            "Не удалось связаться с Telegram: сервер не может подключиться к api.telegram.org. "
            "На VPS в РФ Telegram часто заблокирован — добавьте в .env прокси: "
            "TELEGRAM_PROXY=socks5://логин:пароль@хост:порт (или http://...). "
            f"Подробнее: {message}"
        )
    return f"Не удалось связаться с Telegram: {message}"


def _is_network_unreachable(message: str) -> bool:
    lowered = message.lower()
    return (
        "network is unreachable" in lowered
        or "failed to establish a new connection" in lowered
        or "name or service not known" in lowered
        or "temporary failure in name resolution" in lowered
        or "connection timed out" in lowered
    )


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


def _is_html_parse_error(exc: TelegramError) -> bool:
    msg = str(exc).lower()
    return "parse" in msg or "html" in msg or "can't find end tag" in msg


def _hint_for_telegram_error(description: str, chat_id: str) -> str:
    desc = description.lower()
    if "chat not found" in desc:
        return "Проверьте chat_id и что бот добавлен в чат/группу."
    if "bot was blocked" in desc:
        return "Пользователь заблокировал бота — напишите боту /start."
    if "need administrator" in desc or "not enough rights" in desc:
        return "Добавьте бота в группу/канал с правом отправки сообщений."
    if "wrong chat id" in desc or "group chat was upgraded" in desc:
        return f"Неверный chat_id ({chat_id}). Получите актуальный через getUpdates."
    return "Проверьте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env."


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
