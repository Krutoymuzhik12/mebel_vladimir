"""Кто ведёт чат: только новые диалоги.

Статусы (своя БД):
    new      — клиент написал первым, бот ведёт
    existing — чат уже был до бота / при старте → молчим навсегда
    manual   — менеджер забрал (#стоп или ответ руками) → молчим до #старт
"""

from __future__ import annotations

import logging
from typing import Literal

from app.db.database import Database

logger = logging.getLogger(__name__)

ChatStatus = Literal["new", "existing", "manual"]

BOT_OWNED = "new"
NOT_OURS = "existing"
MANUAL = "manual"

STOP_CMD = "#стоп"
START_CMD = "#старт"


class Gatekeeper:
    def __init__(self, db: Database) -> None:
        self.db = db

    def status(self, chat_id: str) -> ChatStatus | None:
        row = self.db.get_chat(chat_id)
        return row["status"] if row else None  # type: ignore[return-value]

    def bot_may_reply(self, chat_id: str) -> bool:
        return self.status(chat_id) == BOT_OWNED

    def mark_existing(self, chat_id: str, reason: str = "baseline") -> None:
        self.db.upsert_chat(chat_id, status=NOT_OURS)
        logger.info("chat=%s → existing (%s)", chat_id, reason)

    def claim_new(self, chat_id: str) -> None:
        self.db.upsert_chat(chat_id, status=BOT_OWNED)
        logger.info("chat=%s → new (first contact)", chat_id)

    def on_unknown_chat(
        self,
        chat_id: str,
        *,
        channel_id: str = "",
        fresh_channels: set[str] | None = None,
        policy: str = "safe",
        baseline_ready: bool = False,
    ) -> ChatStatus:
        """Незнакомый чат: новый клиент или старый, о котором мы не знаем?

        Отличить по данным Wazzup нельзя — API не отдаёт ни списка чатов, ни
        истории переписки, в вебхуке приходит только само новое сообщение.
        Поэтому решает политика, и по умолчанию она безопасная.

        Цена ошибки несимметрична. Промолчать новому клиенту — потерять
        одного: менеджер увидит чат и ответит руками, как делал раньше.
        Ответить старому — поздороваться «меня зовут Владимир, что
        подбираете?» с человеком, который заказывал три года назад. Второе
        бьёт по репутации сразу и по всем, кто напишет в этот день.

        Исключение — каналы из fresh_channels: там истории до бота нет по
        определению, молчать не от кого.

        baseline_ready говорит, загружен ли список старых чатов из amoCRM.
        Если да — «нет в базе» означает «новый клиент», и боту можно
        отвечать. Если импорт упал, а мы всё равно ответим всем незнакомым,
        то поздороваемся со всей клиентской базой разом.
        """
        current = self.status(chat_id)
        if current:
            return current

        if channel_id and channel_id.lower() in (fresh_channels or set()):
            self.claim_new(chat_id)
            return BOT_OWNED

        mode = (policy or "auto").strip().lower()
        if mode == "auto":
            # База знает историю — значит «нет в базе» честно означает «новый»
            mode = "open" if baseline_ready else "safe"

        if mode == "open":
            self.claim_new(chat_id)
            return BOT_OWNED

        self.mark_existing(
            chat_id, reason="незнакомый чат, база истории не подтверждена"
        )
        return NOT_OURS

    def classify_first_seen(
        self, chat_id: str, *, had_prior_human_outgoing: bool
    ) -> ChatStatus:
        current = self.status(chat_id)
        if current:
            return current
        status: ChatStatus = NOT_OURS if had_prior_human_outgoing else BOT_OWNED
        self.db.upsert_chat(chat_id, status=status)
        logger.info(
            "chat=%s first seen → %s (prior_outgoing=%s)",
            chat_id,
            status,
            had_prior_human_outgoing,
        )
        return status

    def on_staff_message(self, chat_id: str, text: str) -> ChatStatus | None:
        """Сообщение менеджера: #стоп / #старт / ручной ответ."""
        normalized = (text or "").strip().lower()
        current = self.status(chat_id)

        if normalized == STOP_CMD:
            if current == NOT_OURS:
                return current
            self.db.upsert_chat(chat_id, status=MANUAL)
            logger.info("chat=%s → manual (#стоп)", chat_id)
            return MANUAL

        if normalized == START_CMD:
            # existing навсегда молчит — #старт только из manual
            if current == NOT_OURS:
                logger.info("chat=%s #старт ignored (existing forever)", chat_id)
                return NOT_OURS
            if current != MANUAL and current is not None:
                logger.info("chat=%s #старт ignored (status=%s)", chat_id, current)
                return current
            self.db.upsert_chat(chat_id, status=BOT_OWNED)
            logger.info("chat=%s → new (#старт)", chat_id)
            return BOT_OWNED

        if current == BOT_OWNED:
            self.db.upsert_chat(chat_id, status=MANUAL)
            logger.info("chat=%s → manual (staff replied)", chat_id)
            return MANUAL
        return current

    def baseline_many(self, chat_ids: list[str]) -> int:
        """Пометить незнакомые чаты как «были до бота».

        Главная тонкость: коннектор Wazzup заводит контакт в amoCRM на любое
        входящее — в том числе от нового клиента, с которым бот прямо сейчас
        разговаривает. При следующем импорте такой чат придёт в списке
        «старых», и без защиты бот замолчал бы посреди живого диалога.

        Поэтому чат не трогаем, если верен любой из двух независимых
        признаков «диалог наш»:
          1. чат уже есть в базе с любым статусом;
          2. бот в нём уже отвечал — даже если строка chats потерялась.

        Возвращает, сколько реально помечено, а не сколько пришло на вход.
        """
        marked = 0
        kept = 0
        for cid in chat_ids:
            if self.status(cid) is not None:
                continue
            if self.db.bot_has_spoken(cid):
                kept += 1
                logger.info("chat=%s в списке CRM, но диалог наш — не трогаю", cid)
                continue
            self.mark_existing(cid, reason="startup baseline")
            marked += 1
        if kept:
            logger.info("baseline: %s живых диалогов бота сохранено", kept)
        return marked
