"""Клиент прислал документ (PDF/Word/др.) → карточка владельцу в MAX.

Содержимое файла не разбираем — эта фича снята: PDF/DOCX-парсинг был, но
клиент попросил отложить его и вместо этого просто пересылать файл владельцу.
Владелец сам смотрит вложение по ссылке и отвечает текстом, который уходит
клиенту как есть.

Механизм тот же, что у запроса цены (app/jobs/price_relay.py) — карточка с
кнопками «Ответить»/«Пропустить», реплай на карточку тоже работает. Разница
в одном: здесь нет dedup по чату. Цена в чате обычно одна на момент времени,
а файлов клиент может прислать несколько подряд (план + смету), и каждый
должен превратиться в свою карточку, а не потеряться под первой.
"""

from __future__ import annotations

import logging
import re
import uuid
from urllib.parse import unquote, urlparse

from app.config import Settings
from app.db.database import Database
from app.notify.max import MaxNotifier
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)

REQUEST_ID_RE = re.compile(r"request_id:\s*([0-9a-fA-F]{8,})", re.I)


def _name_from_url(url: str) -> str:
    path = urlparse(url or "").path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return name or "файл"


class DocumentRelay:
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

    async def on_client_sent_document(
        self, *, chat_id: str, doc_url: str, note: str = ""
    ) -> str | None:
        """Открыть карточку в MAX. Возвращает request_id, если ушла, иначе None."""
        if not doc_url:
            return None

        request_id = uuid.uuid4().hex[:12]
        doc_name = _name_from_url(doc_url)
        mid = await self.max_notifier.document_request(
            chat_id=chat_id,
            doc_url=doc_url,
            doc_name=doc_name,
            note=note,
            request_id=request_id,
        )
        if not mid:
            logger.warning(
                "document MAX notify failed chat=%s — карточка не открыта", chat_id
            )
            return None

        self.db.open_document_request(
            request_id=request_id,
            chat_id=chat_id,
            doc_url=doc_url,
            doc_name=doc_name,
            note=note,
        )
        self.db.set_document_max_message(request_id, mid)
        return request_id

    async def on_owner_max_message(
        self, text: str, *, request_id: str | None = None
    ) -> bool:
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
            row = self.db.get_document_by_request_id(rid)
            if row and row.get("status") != "pending":
                row = None

        if row is None:
            # безопасный fallback только если ровно один pending во всей системе
            if self.db.count_pending_documents() == 1:
                row = self.db.latest_pending_document()

        if row is None:
            logger.info("MAX document reply ignored: need request_id or single pending")
            return False

        client_text = REQUEST_ID_RE.sub("", body).strip() or body
        chat_id = row["chat_id"]
        result = await self.wazzup.send_text(chat_id, client_text)
        if result.ok:
            self.db.close_document_request(row["request_id"], delivered=True)
            self.db.add_message(chat_id, role="assistant", text=client_text)
            self.db.touch_bot_message(chat_id)
            if self.settings.max_reply_takes_over:
                # Владелец ответил лично — дальше ведёт он, бот молчит
                self.db.upsert_chat(chat_id, status="manual")
                logger.info(
                    "chat=%s → manual (ответ владельца из MAX)", chat_id
                )
            logger.info(
                "document relayed request_id=%s chat=%s", row["request_id"], chat_id
            )
            return True

        logger.info(
            "document relay failed request_id=%s err=%s",
            row["request_id"],
            result.error,
        )
        return False
