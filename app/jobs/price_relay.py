"""Цепочка «клиент ждёт цену» ↔ ответ владельца в MAX.

1. Intent → пушим в MAX; pending открываем только после успешного пуша.
2. Владелец отвечает с request_id (или единственный pending).
3. Текст as-is → клиенту в Wazzup, чат уходит в manual.

Ответ владельца из MAX = перехват (MAX_REPLY_TAKES_OVER=1). Владелец
уже лично написал клиенту, значит дальше ведёт разговор он, и бот в
этом чате замолкает. Иначе получилось бы, что после цены от хозяина
бот продолжает говорить поверх него.
"""

from __future__ import annotations

import logging
import re
import uuid

from app.config import Settings
from app.db.database import Database
from app.notify.max import MaxNotifier
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)

REQUEST_ID_RE = re.compile(r"request_id:\s*([0-9a-fA-F]{8,})", re.I)


class PriceRelay:
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

    async def on_client_wants_price(
        self,
        *,
        chat_id: str,
        summary: str,
        ask: str,
    ) -> str | None:
        existing = self.db.get_pending_price(chat_id)
        if existing:
            logger.info(
                "price already pending chat=%s request_id=%s",
                chat_id,
                existing["request_id"],
            )
            return existing["request_id"]

        request_id = uuid.uuid4().hex[:12]
        mid = await self.max_notifier.price_request(
            chat_id=chat_id,
            summary=summary,
            ask=ask,
            request_id=request_id,
        )
        if not mid:
            logger.warning("price MAX notify failed chat=%s — pending not opened", chat_id)
            return None

        self.db.open_price_request(
            request_id=request_id,
            chat_id=chat_id,
            summary=summary,
            ask=ask,
        )
        # mid карточки: по нему поймём, на какую заявку владелец ответил реплаем
        self.db.set_price_max_message(request_id, mid)
        return request_id

    async def on_owner_max_message(self, text: str, *, request_id: str | None = None) -> bool:
        body = (text or "").strip()
        if not body:
            return False

        rid = request_id
        if not rid:
            m = REQUEST_ID_RE.search(body)
            if m:
                rid = m.group(1)

        row = None
        if rid:
            row = self.db.get_price_by_request_id(rid)
            if row and row.get("status") != "pending":
                row = None

        if row is None:
            # безопасный fallback только если ровно один pending во всей системе
            if self.db.count_pending_prices() == 1:
                row = self.db.latest_pending_price()

        if row is None:
            logger.info("MAX price reply ignored: need request_id or single pending")
            return False

        # убрать служебную строку request_id из текста клиенту
        client_text = REQUEST_ID_RE.sub("", body).strip() or body
        chat_id = row["chat_id"]
        result = await self.wazzup.send_text(chat_id, client_text)
        if result.ok:
            self.db.close_price_request(row["request_id"], delivered=True)
            self.db.add_message(chat_id, role="assistant", text=client_text)
            self.db.touch_bot_message(chat_id)
            if self.settings.max_reply_takes_over:
                # Владелец ответил лично — дальше ведёт он, бот молчит
                self.db.upsert_chat(chat_id, status="manual")
                logger.info(
                    "chat=%s → manual (ответ владельца из MAX)", chat_id
                )
            logger.info("price relayed request_id=%s chat=%s", row["request_id"], chat_id)
            return True

        logger.info(
            "price relay failed request_id=%s err=%s",
            row["request_id"],
            result.error,
        )
        return False
