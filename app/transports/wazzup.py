"""Адаптер Wazzup API — основной I/O чатов.

Сюда: webhook/вход входящих + отправка ответов/медиа.
В том числе системные события Авито («показ номера» без звонка) приходят сюда.
amoCRM в коде сейчас не участвует.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.transports.base import IncomingMessage, SendResult

logger = logging.getLogger(__name__)


class WazzupTransport:
    name = "wazzup"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.wazzup_api_key)

    def parse_webhook(self, payload: dict[str, Any]) -> list[IncomingMessage]:
        """Разбор webhook → нормализованные сообщения. Формат уточним по ключам."""
        _ = payload
        return []

    def looks_like_avito_show_phone(self, msg: IncomingMessage) -> bool:
        """Системное событие Авито «клиент показал номер».

        Нужны: канал avito + is_system + маркеры в тексте.
        """
        channel = (msg.channel or "").lower()
        if "avito" not in channel or not msg.is_system:
            return False
        text = (msg.text or "").lower()
        needles = (
            "показ номера",
            "показал номер",
            "показала номер",
            "show phone",
            "просмотр номера",
        )
        return any(n in text for n in needles)

    async def send_text(self, chat_id: str, text: str) -> SendResult:
        if not self.configured:
            logger.warning("wazzup send skipped (no API key) chat=%s", chat_id)
            return SendResult(ok=False, error="wazzup not configured")
        # TODO: POST Wazzup API → сообщение в chat_id
        logger.info("wazzup send_text stub chat=%s len=%s", chat_id, len(text))
        return SendResult(ok=False, error="not implemented")

    async def send_media(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> SendResult:
        if not self.configured:
            return SendResult(ok=False, error="wazzup not configured")
        logger.info(
            "wazzup send_media stub chat=%s file=%s caption_len=%s",
            chat_id,
            file_path,
            len(caption),
        )
        return SendResult(ok=False, error="not implemented")

    async def baseline_existing_chats(self) -> list[str]:
        """Список chat_id до старта бота → status=existing."""
        return []
