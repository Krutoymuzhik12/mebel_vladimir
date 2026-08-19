"""Дожимы: напоминание клиенту, который перестал отвечать.

Текст и задержка зависят от того, ПОЧЕМУ клиент замолчал (см. app/core/stall.py):
ушедшему с возражением и сказавшему «оформляйте» нужны разные слова, а тому,
кто ждёт от нас расчёт, писать нельзя вовсе — вместо этого пинаем владельца.

Отдельно от лестницы стоит договорённость о сроке: если клиент попросил
вернуться через неделю, никакие ступени не действуют до этого срока. Такую
просьбу ставит менеджер-бот маркером [[ОТЛОЖИТЬ: N | причина]].

Ночью не пушим: за тихие часы отвечает QuietHours.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import Settings
from app.core import stall
from app.core.quiet_hours import QuietHours
from app.db.database import Database
from app.notify.max import MaxNotifier
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)

# Сколько часов ждём расчёт от владельца, прежде чем напомнить ему самому
PRICE_OVERDUE_HOURS = 2.0


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
            text, kind = self._pick_message(row)
            if not text:
                continue

            result = await self.wazzup.send_text(chat_id, text)
            if not result.ok:
                logger.info("followup not sent chat=%s err=%s", chat_id, result.error)
                continue

            self.db.add_message(chat_id, role="assistant", text=text)
            stage = int(row.get("followup_stage") or 0)
            if kind == "snooze":
                # Договорённость исполнена. Дальше человек либо ответит, либо
                # получит одно мягкое напоминание — но не всю лестницу заново.
                self.db.clear_snooze(chat_id)
                self.db.record_followup_sent(chat_id, stage=1)
                logger.info("followup chat=%s возврат по договорённости", chat_id)
            else:
                self.db.record_followup_sent(chat_id, stage=stage + 1)
                logger.info(
                    "followup chat=%s причина=%s ступень=%s→%s",
                    chat_id, kind, stage, stage + 1,
                )
            sent += 1
        return sent

    def _pick_message(self, row: dict) -> tuple[str | None, str]:
        """Что и почему отправляем этому чату."""
        chat_id = row["chat_id"]

        # Договорённость о сроке сильнее любой лестницы
        due_raw = row.get("followup_due_at")
        if due_raw:
            due = _parse_iso(due_raw)
            if due is None:
                self.db.clear_snooze(chat_id)
            elif datetime.now(timezone.utc) < due:
                return None, "snooze_waiting"
            else:
                return stall.snoozed_return(row.get("snooze_reason")), "snooze"

        has_pending = bool(self.db.get_pending_price(chat_id))
        reason = stall.reason_for(row.get("last_intent"), has_pending_price=has_pending)
        text = stall.next_followup(
            reason,
            stage=int(row.get("followup_stage") or 0),
            silent_hours=float(row.get("silent_hours") or 0.0),
            base_hours=self.settings.followup_silence_hours,
        )
        return text, reason

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
