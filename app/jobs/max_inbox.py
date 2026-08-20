"""Входящее из MAX: нажатия кнопок и ответы сотрудников.

Long polling вместо вебхука: отдельный публичный адрес не нужен, а живёт цикл
в том же процессе, что и остальные фоновые задачи.

Через MAX сейчас идут два вида карточек — запрос цены (PriceRelay) и
пересланный клиентский файл (DocumentRelay). Кнопки и текст у них одинаковые
(«Ответить» / «Пропустить», payload = префикс + request_id), поэтому кнопку
не привязываем к конкретному релею заранее: по request_id сначала смотрим
среди цен, не нашли — среди документов. request_id — uuid4 в обоих релеях,
так что совпадение чужого типа исключено практически полностью.

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
from typing import Any, Literal

from app.config import ROOT, Settings
from app.db.database import Database
from app.jobs.document_relay import DocumentRelay
from app.jobs.price_relay import PriceRelay
from app.notify.max import ANSWER_PREFIX, SKIP_PREFIX, MaxNotifier
from app.services.process_lock import ExclusiveLock

logger = logging.getLogger(__name__)

POLL_TIMEOUT = 30
ERROR_BACKOFF = 5.0

Kind = Literal["price", "document"]


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
        document_relay: DocumentRelay | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.max = max_notifier
        self.price_relay = price_relay
        self.document_relay = document_relay
        self._marker: int | None = None

    async def run_forever(self) -> None:
        if not self.max.ready:
            logger.info("MAX inbox не запущен: нет токена или чата")
            return

        lock_path = ROOT / self.settings.max_inbox_lock_path
        lock = ExclusiveLock(lock_path)
        if not lock.acquire():
            logger.error(
                "MAX inbox: лок %s занят — второй процесс не слушаю "
                "(иначе события кнопок разъедутся)",
                lock_path,
            )
            return

        logger.info("MAX inbox: слушаю чат %s (лок %s)", self.max.chat_id, lock_path)
        try:
            while True:
                try:
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MAX inbox: цикл упал, пробуем снова")
                    await asyncio.sleep(ERROR_BACKOFF)
        finally:
            lock.release()

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

    # ---------- поиск заявки: цена или документ ----------

    def _pending_by_request_id(
        self, request_id: str
    ) -> tuple[Kind, dict[str, Any]] | tuple[None, None]:
        row = self.db.get_price_by_request_id(request_id)
        if row and row.get("status") == "pending":
            return "price", row
        row = self.db.get_document_by_request_id(request_id)
        if row and row.get("status") == "pending":
            return "document", row
        return None, None

    def _pending_by_max_message(
        self, mid: str
    ) -> tuple[Kind, dict[str, Any]] | tuple[None, None]:
        row = self.db.get_price_by_max_message(mid)
        if row and row.get("status") == "pending":
            return "price", row
        row = self.db.get_document_by_max_message(mid)
        if row and row.get("status") == "pending":
            return "document", row
        return None, None

    # ---------- нажатие кнопки ----------

    async def _on_callback(self, update: dict[str, Any]) -> None:
        callback = update.get("callback") or {}
        callback_id = str(callback.get("callback_id") or "")
        payload = str(callback.get("payload") or "")
        chat_id = _chat_id_of(update)
        user_id = _user_id_of(update)

        if payload.startswith(ANSWER_PREFIX):
            request_id = payload[len(ANSWER_PREFIX) :]
            kind, row = self._pending_by_request_id(request_id)
            if row is None:
                await self.max.answer_callback(
                    callback_id, "Эта заявка уже закрыта"
                )
                return
            self.db.set_awaiting(chat_id, user_id, request_id)
            logger.info(
                "MAX: сотрудник %s отвечает на заявку %s (%s)",
                user_id,
                request_id,
                kind,
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
            kind, _row = self._pending_by_request_id(request_id)
            if kind == "document":
                self.db.skip_document_request(request_id)
            else:
                self.db.skip_price_request(request_id)
            self.db.clear_awaiting_for_request(request_id)
            logger.info("MAX: заявка %s (%s) пропущена", request_id, kind or "?")
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
        kind, request_id = self._request_for(update, chat_id, user_id)
        if not request_id:
            # обычная переписка сотрудников — не наше дело
            return

        if kind == "document" and self.document_relay is not None:
            row = self.db.get_document_by_request_id(request_id)
            client_chat = str((row or {}).get("chat_id") or "?")
            ok = await self.document_relay.on_owner_max_message(
                text, request_id=request_id
            )
        else:
            row = self.db.get_price_by_request_id(request_id)
            client_chat = str((row or {}).get("chat_id") or "?")
            ok = await self.price_relay.on_owner_max_message(
                text, request_id=request_id
            )

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
    ) -> tuple[Kind, str] | tuple[None, None]:
        """Какой заявке адресован этот текст: (тип, request_id)."""
        # Реплай на карточку — однозначно, состояние не нужно
        reply_mid = _reply_to_mid(update)
        if reply_mid:
            kind, row = self._pending_by_max_message(reply_mid)
            if row is not None:
                return kind, str(row["request_id"])

        # Иначе — заявка, на которую этот сотрудник нажал «Ответить»
        awaiting = self.db.get_awaiting(
            chat_id, user_id, ttl_minutes=self.settings.max_awaiting_ttl_min
        )
        if not awaiting:
            return None, None
        kind, row = self._pending_by_request_id(awaiting)
        if row is None:
            return None, None
        return kind, awaiting
