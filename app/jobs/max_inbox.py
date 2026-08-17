"""Входящее из MAX: нажатия кнопок и ответы сотрудников.

Long polling вместо вебхука: отдельный публичный адрес не нужен, а живёт цикл
в том же процессе, что и остальные фоновые задачи.

Как заявка находит свой ответ — два пути, в порядке надёжности:

1. Сотрудник ответил РЕПЛАЕМ на карточку. Тогда заявка известна точно, и
   никакое состояние не нужно.
2. Сотрудник нажал «Ответить» и пишет следующим сообщением. Ожидание хранится
   в БД по паре (чат, сотрудник), а не по заявке — поэтому новые заявки,
   прилетевшие пока он печатает, ничего не сбивают: у них своя карточка со
   своими кнопками, а его режим ожидания остаётся на той заявке, которую он
   выбрал. Второй сотрудник в том же чате параллельно отвечает на свою.

Ожидание протухает через MAX_AWAITING_TTL_MIN: забытое нажатие «Ответить» не
должно через сутки перехватить постороннюю реплику и уехать клиенту.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import Settings
from app.db.database import Database
from app.jobs.price_relay import PriceRelay
from app.notify.max import ANSWER_PREFIX, SKIP_PREFIX, MaxNotifier

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30
ERROR_BACKOFF = 5.0


def _chat_id_of(update: dict[str, Any]) -> str:
    message = update.get("message") or {}
    recipient = message.get("recipient") or {}
    return str(recipient.get("chat_id") or update.get("chat_id") or "")


def _user_id_of(update: dict[str, Any]) -> str:
    callback = update.get("callback") or {}
    user = (
        callback.get("user")
        or (update.get("message") or {}).get("sender")
        or update.get("user")
        or {}
    )
    return str(user.get("user_id") or "")


def _text_of(update: dict[str, Any]) -> str:
    body = (update.get("message") or {}).get("body") or {}
    return str(body.get("text") or "").strip()


def _reply_to_mid(update: dict[str, Any]) -> str:
    """mid сообщения, на которое ответили реплаем."""
    link = (update.get("message") or {}).get("link") or {}
    linked = link.get("message") or {}
    return str(linked.get("mid") or "")


class MaxInbox:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        max_notifier: MaxNotifier,
        price_relay: PriceRelay,
    ) -> None:
        self.settings = settings
        self.db = db
        self.max = max_notifier
        self.price_relay = price_relay
        self._marker: int | None = None

    async def run_forever(self) -> None:
        if not self.max.ready:
            logger.info("MAX inbox не запущен: нет токена или чата")
            return
        logger.info("MAX inbox: слушаю чат %s", self.max.chat_id)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("MAX inbox: цикл упал, пробуем снова")
                await asyncio.sleep(ERROR_BACKOFF)

    async def poll_once(self) -> None:
        data = await self.max.get_updates(self._marker, timeout=POLL_TIMEOUT)
        if data is None:
            await asyncio.sleep(ERROR_BACKOFF)
            return
        self._marker = data.get("marker") or self._marker
        for update in data.get("updates") or []:
            try:
                await self.handle_update(update)
            except Exception:
                logger.exception("MAX inbox: событие не обработано")

    async def handle_update(self, update: dict[str, Any]) -> None:
        kind = str(update.get("update_type") or "")
        if self.settings.log_raw_webhook:
            logger.info("MAX raw update: %s", update)
        if kind == "message_callback":
            await self._on_callback(update)
        elif kind == "message_created":
            await self._on_message(update)

    # ---------- нажатие кнопки ----------

    async def _on_callback(self, update: dict[str, Any]) -> None:
        callback = update.get("callback") or {}
        callback_id = str(callback.get("callback_id") or "")
        payload = str(callback.get("payload") or "")
        chat_id = _chat_id_of(update)
        user_id = _user_id_of(update)

        if payload.startswith(ANSWER_PREFIX):
            request_id = payload[len(ANSWER_PREFIX) :]
            row = self.db.get_price_by_request_id(request_id)
            if not row or row.get("status") != "pending":
                await self.max.answer_callback(
                    callback_id, "Эта заявка уже закрыта"
                )
                return
            self.db.set_awaiting(chat_id, user_id, request_id)
            logger.info(
                "MAX: сотрудник %s отвечает на заявку %s", user_id, request_id
            )
            await self.max.answer_callback(callback_id, "Жду ваш ответ")
            # Всплывашку из answer_callback MAX не показывает, поэтому пишем
            # обычным сообщением — иначе нажатие выглядит как «ничего не
            # произошло», и сотрудник жмёт кнопку повторно.
            await self.max.send(
                "✍️ Жду ваш ответ по заявке "
                f"{request_id}\nКлиент: {row.get('chat_id')}\n\n"
                "Напишите сообщение — отправлю его клиенту как есть. "
                "Чтобы отменить, нажмите «Пропустить» на карточке."
            )
            return

        if payload.startswith(SKIP_PREFIX):
            request_id = payload[len(SKIP_PREFIX) :]
            self.db.skip_price_request(request_id)
            self.db.clear_awaiting_for_request(request_id)
            logger.info("MAX: заявка %s пропущена", request_id)
            await self.max.answer_callback(
                callback_id, "Пропущено — клиенту ничего не отправлено"
            )

    # ---------- сообщение сотрудника ----------

    async def _on_message(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        sender = message.get("sender") or {}
        if sender.get("is_bot"):
            return

        chat_id = _chat_id_of(update)
        if chat_id != str(self.max.chat_id):
            return

        text = _text_of(update)
        if not text:
            return

        user_id = _user_id_of(update)
        request_id = self._request_for(update, chat_id, user_id)
        if not request_id:
            # обычная переписка сотрудников — не наше дело
            return

        row = self.db.get_price_by_request_id(request_id)
        client_chat = str((row or {}).get("chat_id") or "?")

        ok = await self.price_relay.on_owner_max_message(text, request_id=request_id)
        if ok:
            self.db.clear_awaiting(chat_id, user_id)
            await self.max.send(f"✅ Отправлено клиенту {client_chat}")
        else:
            await self.max.send(
                f"⚠️ Не удалось отправить клиенту {client_chat} "
                f"(заявка {request_id}). Проверьте, что чат ещё активен."
            )

    def _request_for(
        self, update: dict[str, Any], chat_id: str, user_id: str
    ) -> str | None:
        """Какой заявке адресован этот текст."""
        # Реплай на карточку — однозначно, состояние не нужно
        reply_mid = _reply_to_mid(update)
        if reply_mid:
            row = self.db.get_price_by_max_message(reply_mid)
            if row and row.get("status") == "pending":
                return str(row["request_id"])

        # Иначе — заявка, на которую этот сотрудник нажал «Ответить»
        return self.db.get_awaiting(
            chat_id, user_id, ttl_minutes=self.settings.max_awaiting_ttl_min
        )
