"""Авито: клиент нажал «показать номер», звонка не было.

Событие в Wazzup → уведомление в группу MAX.
Клиенту НЕ отвечаем: он смотрел номер, а не писал, и любое сообщение от нас
выглядело бы слежкой. Дальше владелец перезванивает сам.

Флаг notified ставим только после успешного пуша в MAX, поэтому упавшее
уведомление дошлёт tick(). Выключается через MAX_NOTIFY_AVITO=0.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.db.database import Database
from app.notify.max import MaxNotifier
from app.transports.base import IncomingMessage
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)


class AvitoShowPhoneJob:
    def __init__(
        self,
        db: Database,
        wazzup: WazzupTransport,
        max_notifier: MaxNotifier,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.wazzup = wazzup
        self.max_notifier = max_notifier
        self.settings = settings or max_notifier.settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.max_notify_avito)

    async def on_event(self, msg: IncomingMessage) -> None:
        if not self.wazzup.looks_like_avito_show_phone(msg):
            return
        if not self.enabled:
            # Ничего не помечаем: иначе tick() будет вечно пытаться
            # дослать это в MAX, раз notified так и не проставится.
            logger.info(
                "avito show-phone chat=%s — уведомления выключены", msg.chat_id
            )
            return
        logger.info("avito show-phone → MAX chat=%s", msg.chat_id)
        self.db.mark_show_phone(msg.chat_id)
        ok = await self.max_notifier.avito_show_phone(
            chat_id=msg.chat_id,
            details=msg.text or "",
        )
        if ok:
            self.db.mark_show_phone_notified(msg.chat_id)
        else:
            logger.warning(
                "avito show-phone MAX failed chat=%s — leave for retry",
                msg.chat_id,
            )

    async def tick(self) -> int:
        """Повтор недоставленных уведомлений в MAX."""
        if not self.enabled:
            return 0
        done = 0
        for row in self.db.candidates_show_phone():
            ok = await self.max_notifier.avito_show_phone(
                chat_id=row["chat_id"],
                details="",
            )
            if ok:
                self.db.mark_show_phone_notified(row["chat_id"])
                done += 1
        return done
