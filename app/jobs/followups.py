"""Push: напоминание, если клиент молчит N часов после ответа бота.

Учитывает тихие часы: ночью не пушим.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.core.quiet_hours import QuietHours
from app.db.database import Database
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)

DEFAULT_REMINDER = (
    "Здравствуйте! Подскажите, остались ли у вас вопросы? "
    "Могу подсказать по материалам, срокам или сориентировать по стоимости."
)


class FollowUpJob:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        wazzup: WazzupTransport,
        quiet: QuietHours,
    ) -> None:
        self.settings = settings
        self.db = db
        self.wazzup = wazzup
        self.quiet = quiet

    async def tick(self) -> int:
        if not self.quiet.can_push():
            logger.debug("followup skip: quiet hours")
            return 0
        sent = 0
        for row in self.db.candidates_for_followup(self.settings.followup_silence_hours):
            chat_id = row["chat_id"]
            result = await self.wazzup.send_text(chat_id, DEFAULT_REMINDER)
            if result.ok:
                self.db.add_message(chat_id, role="assistant", text=DEFAULT_REMINDER)
                self.db.record_followup_sent(chat_id, stage=1)
                sent += 1
            else:
                logger.info("followup not sent chat=%s err=%s", chat_id, result.error)
        return sent
