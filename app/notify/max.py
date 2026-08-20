"""MAX Bot API: заявки владельцу с кнопками «Ответить» / «Пропустить».

Сценарий: сработал интент «клиент ждёт цену» → в чат MAX уходит карточка с
конспектом и двумя кнопками. «Ответить» переводит бота в режим ожидания текста
от этого сотрудника; следующее его сообщение уходит клиенту как есть.
«Пропустить» закрывает заявку — клиент возвращается в обычные дожимы.

База API: https://botapi.max.ru, токен передаётся заголовком Authorization
(query-параметр access_token объявлен устаревшим и отдаёт 401).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

API_BASE = "https://botapi.max.ru"
REQUEST_TIMEOUT = 30.0

ANSWER_PREFIX = "answer:"
SKIP_PREFIX = "skip:"


class MaxNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: httpx.AsyncClient | None = None

    @property
    def ready(self) -> bool:
        return bool(
            self.settings.max_enabled
            and self.settings.max_bot_token
            and self.chat_id
        )

    @property
    def chat_id(self) -> int:
        raw = str(self.settings.max_group_id or "").strip()
        try:
            return int(raw)
        except ValueError:
            return 0

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=API_BASE,
                timeout=REQUEST_TIMEOUT,
                headers={"Authorization": self.settings.max_bot_token},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    # ---------- низкий уровень ----------

    async def _post(
        self, path: str, *, params: dict[str, Any] | None = None, json: Any = None
    ) -> dict[str, Any] | None:
        try:
            client = await self.client()
            resp = await client.post(path, params=params, json=json)
            if resp.status_code >= 400:
                logger.warning(
                    "MAX %s -> %s: %s", path, resp.status_code, resp.text[:300]
                )
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError):
            logger.exception("MAX: запрос %s не удался", path)
            return None

    async def get_updates(
        self, marker: int | None, *, timeout: int = 30, limit: int = 50
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker:
            params["marker"] = marker
        try:
            client = await self.client()
            # long polling: ждём дольше, чем обычный запрос
            resp = await client.get(
                "/updates", params=params, timeout=timeout + REQUEST_TIMEOUT
            )
            if resp.status_code >= 400:
                logger.warning("MAX /updates -> %s: %s", resp.status_code, resp.text[:300])
                return None
            return resp.json()
        except (httpx.HTTPError, ValueError):
            logger.exception("MAX: не удалось получить обновления")
            return None

    async def send(
        self,
        text: str,
        *,
        buttons: list[list[dict[str, Any]]] | None = None,
    ) -> str:
        """Отправить в рабочий чат. Возвращает mid сообщения или пустую строку."""
        if not self.ready:
            logger.info("MAX выключен (MAX_ENABLED/токен/чат) | %s", text[:200])
            return ""
        body: dict[str, Any] = {"text": text}
        if buttons:
            body["attachments"] = [
                {"type": "inline_keyboard", "payload": {"buttons": buttons}}
            ]
        data = await self._post("/messages", params={"chat_id": self.chat_id}, json=body)
        if not data:
            return ""
        mid = str(((data.get("message") or {}).get("body") or {}).get("mid") or "")
        logger.info("MAX отправлено mid=%s", mid or "?")
        return mid

    async def answer_callback(self, callback_id: str, notification: str = "") -> bool:
        """Погасить «часики» на кнопке. Без этого MAX считает нажатие незавершённым."""
        if not callback_id:
            return False
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification
        data = await self._post(
            "/answers", params={"callback_id": callback_id}, json=body
        )
        return bool(data)

    # ---------- прикладное ----------

    async def price_request(
        self,
        *,
        chat_id: str,
        summary: str,
        ask: str,
        request_id: str,
    ) -> str:
        """Карточка расчёта с кнопками. Возвращает mid — по нему ловим reply."""
        body = (
            "💰 Клиент ждёт расчёт\n"
            f"Чат: {chat_id}\n"
            f"request_id: {request_id}\n\n"
            f"Запрос:\n{ask}\n\n"
            f"Переписка:\n{summary}"
        )
        buttons = [
            [
                {
                    "type": "callback",
                    "text": "✍️ Ответить",
                    "payload": f"{ANSWER_PREFIX}{request_id}",
                    "intent": "positive",
                },
                {
                    "type": "callback",
                    "text": "Пропустить",
                    "payload": f"{SKIP_PREFIX}{request_id}",
                    "intent": "negative",
                },
            ]
        ]
        return await self.send(body, buttons=buttons)

    async def price_overdue(
        self,
        *,
        chat_id: str,
        request_id: str,
        ask: str,
        age_hours: float,
    ) -> bool:
        """Расчёт висит без ответа — клиента мы при этом не дёргаем."""
        body = (
            "⏰ Расчёт всё ещё не отправлен\n"
            f"Чат: {chat_id}\n"
            f"request_id: {request_id}\n"
            f"Ждёт: {age_hours:.1f} ч\n\n"
            f"Запрос:\n{ask}\n\n"
            "Клиенту мы ничего не писали и напоминать ему не будем."
        )
        buttons = [
            [
                {
                    "type": "callback",
                    "text": "✍️ Ответить",
                    "payload": f"{ANSWER_PREFIX}{request_id}",
                    "intent": "positive",
                },
                {
                    "type": "callback",
                    "text": "Пропустить",
                    "payload": f"{SKIP_PREFIX}{request_id}",
                    "intent": "negative",
                },
            ]
        ]
        return bool(await self.send(body, buttons=buttons))

    async def document_request(
        self,
        *,
        chat_id: str,
        doc_url: str,
        doc_name: str,
        note: str,
        request_id: str,
    ) -> str:
        """Клиент прислал файл — карточка со ссылкой, содержимое не разбираем.

        Кнопки и обработка ответа те же, что у запроса цены (см.
        app/jobs/max_inbox.py): «Ответить» переводит в режим ожидания текста
        для этого сотрудника, «Пропустить» закрывает без ответа клиенту.
        """
        body = (
            "📎 Клиент прислал файл\n"
            f"Чат: {chat_id}\n"
            f"request_id: {request_id}\n\n"
            f"Файл: {doc_name}\n{doc_url}\n"
        )
        if note:
            body += f"\nСообщение клиента:\n{note}"
        buttons = [
            [
                {
                    "type": "callback",
                    "text": "✍️ Ответить",
                    "payload": f"{ANSWER_PREFIX}{request_id}",
                    "intent": "positive",
                },
                {
                    "type": "callback",
                    "text": "Пропустить",
                    "payload": f"{SKIP_PREFIX}{request_id}",
                    "intent": "negative",
                },
            ]
        ]
        return await self.send(body, buttons=buttons)

    async def avito_show_phone(self, *, chat_id: str, details: str = "") -> bool:
        """Клиенту не пишем — только уведомление владельцу."""
        body = f"📞 Авито: клиент посмотрел номер и не позвонил\nЧат: {chat_id}\n"
        if details:
            body += f"\n{details}"
        return bool(await self.send(body))
