"""Диалоговое ядро — заглушка до ключей Poe.

В Manager уйдут последние HISTORY_LIMIT (40) сообщений из БД.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.config import settings
from app.core.markers import Markers, extract
from app.db.database import Database
from app.transports.base import IncomingMessage

logger = logging.getLogger(__name__)


@dataclass
class DialogResult:
    reply: str
    markers: Markers


class DialogService:
    def __init__(self, db: Database | None = None) -> None:
        self.history_limit = settings.history_limit
        self.db = db

    async def handle(
        self, chat_id: str, messages: list[IncomingMessage]
    ) -> DialogResult:
        history: list[dict] = []
        if self.db is not None:
            history = self.db.recent_messages(chat_id, self.history_limit)
        logger.info(
            "dialog stub chat=%s batch=%s history=%s limit=%s",
            chat_id,
            len(messages),
            len(history),
            self.history_limit,
        )
        raw = ""
        clean, markers = extract(raw)
        return DialogResult(reply=clean, markers=markers)
