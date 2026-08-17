"""Дожимы: напоминание клиенту, который перестал отвечать.

Текст и задержка зависят от того, ПОЧЕМУ клиент замолчал (см. app/core/stall.py):
ушедшему с возражением и сказавшему «оформляйте» нужны разные слова, а тому,
кто ждёт от нас расчёт, писать нельзя вовсе — вместо этого пинаем владельца.

Ночью не пушим: за тихие часы отвечает QuietHours.
"""

from __future__ import annotations

import logging

from app.config import Settings
from app.core import stall
from app.core.quiet_hours import QuietHours
from app.db.database import Database
from app.notify.max import MaxNotifier
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)

# Сколько часов ждём расчёт от владельца, прежде чем напомнить ему самому
PRICE_OVERDUE_HOURS = 2.0


class FollowUpJob:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        wazzup: WazzupTransport,
        quiet: QuietHours,
        max_notifier: MaxNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.wazzup = wazzup
        self.quiet = quiet
        self.max_notifier = max_notifier

    async def tick(self) -> int:
        if not self.quiet.can_push():
            logger.debug("followup skip: quiet hours")
            return 0

        await self._remind_owner_about_prices()

        sent = 0
        for row in self.db.candidates_for_followup(max_stage=self._max_stage()):
            chat_id = row["chat_id"]
            stage = int(row.get("followup_stage") or 0)
            has_pending = bool(self.db.get_pending_price(chat_id))
            reason = stall.reason_for(
                row.get("last_intent"), has_pending_price=has_pending
            )
            text = stall.next_followup(
                reason,
                stage=stage,
                silent_hours=float(row.get("silent_hours") or 0.0),
                base_hours=self.settings.followup_silence_hours,
            )
            if not text:
                continue

            result = await self.wazzup.send_text(chat_id, text)
            if not result.ok:
                logger.info("followup not sent chat=%s err=%s", chat_id, result.error)
                continue

            self.db.add_message(chat_id, role="assistant", text=text)
            self.db.record_followup_sent(chat_id, stage=stage + 1)
            sent += 1
            logger.info(
                "followup chat=%s причина=%s ступень=%s→%s",
                chat_id,
                reason,
                stage,
                stage + 1,
            )
        return sent

    def _max_stage(self) -> int:
        """Самая длинная цепочка среди причин — дальше отбор идёт по причине."""
        return max(stall.max_stage(r) for r in stall.ALL_REASONS)

    async def _remind_owner_about_prices(self) -> None:
        """Клиент ждёт цену дольше положенного — виноваты мы, не клиент."""
        if self.max_notifier is None:
            return
        for row in self.db.stale_pending_prices(PRICE_OVERDUE_HOURS):
            if self.db.price_reminder_sent(row["request_id"]):
                continue
            ok = await self.max_notifier.price_overdue(
                chat_id=row["chat_id"],
                request_id=row["request_id"],
                ask=row.get("ask") or "",
                age_hours=float(row.get("age_hours") or 0.0),
            )
            if ok:
                self.db.mark_price_reminder_sent(row["request_id"])
