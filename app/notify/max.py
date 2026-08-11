"""MAX через личный аккаунт.

Сюда уходит:
- клиент ждёт цену (intent);
- Авито: клиент показал номер и не позвонил.

Обратный путь по цене: ответ владельца в группе → PriceRelay → клиенту в Wazzup.
"""

from __future__ import annotations

import logging

from app.config import Settings

logger = logging.getLogger(__name__)


class MaxNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def ready(self) -> bool:
        return bool(self.settings.max_enabled and self.settings.max_group_id)

    async def send(self, text: str) -> bool:
        if not self.ready:
            logger.info("MAX stub (disabled) | %s", text[:200])
            return False
        # TODO: личный аккаунт MAX → группа MAX_GROUP_ID
        logger.info("MAX send stub group=%s len=%s", self.settings.max_group_id, len(text))
        return False

    async def price_request(
        self,
        *,
        chat_id: str,
        summary: str,
        ask: str,
        request_id: str,
    ) -> bool:
        body = (
            "💰 Клиент ждёт цену\n"
            f"request_id: {request_id}\n"
            f"Чат: {chat_id}\n\n"
            f"Конспект:\n{summary}\n\n"
            f"Запрос:\n{ask}\n\n"
            "Ответьте в этот чат сообщением с ценой — "
            "бот отправит его клиенту как есть."
        )
        return await self.send(body)

    async def avito_show_phone(self, *, chat_id: str, details: str = "") -> bool:
        """Клиенту не пишем — только уведомление владельцу в MAX."""
        body = (
            "📞 Авито: клиент посмотрел номер и не позвонил\n"
            f"Чат: {chat_id}\n"
        )
        if details:
            body += f"\n{details}"
        return await self.send(body)
