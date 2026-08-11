"""Оркестратор: Wazzup → gatekeeper → batcher → dialog → reply.

Ключи Wazzup/Poe/MAX подключим позже; каркас уже гоняет безопасный путь.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

from app.config import Settings
from app.core.batcher import MessageBatcher
from app.core.dialog import DialogService
from app.core.gatekeeper import BOT_OWNED, Gatekeeper
from app.core.quiet_hours import QuietHours
from app.db.database import Database
from app.jobs.avito_show_phone import AvitoShowPhoneJob
from app.jobs.followups import FollowUpJob
from app.jobs.price_relay import PriceRelay
from app.notify.max import MaxNotifier
from app.transports.base import IncomingMessage
from app.transports.wazzup import WazzupTransport

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings, db: Database, wazzup: WazzupTransport) -> None:
        self.settings = settings
        self.db = db
        self.wazzup = wazzup
        self.quiet = QuietHours(settings)
        self.gate = Gatekeeper(db)
        self.dialog = DialogService(db)
        self.max_notifier = MaxNotifier(settings)
        self.price_relay = PriceRelay(db, wazzup, self.max_notifier)
        self.avito_job = AvitoShowPhoneJob(db, wazzup, self.max_notifier)
        self.followup_job = FollowUpJob(settings, db, wazzup, self.quiet)
        self.batcher = MessageBatcher(settings, self._on_batch)
        self._bg_tasks: list[asyncio.Task] = []

    async def startup(self) -> None:
        chat_ids = await self.wazzup.baseline_existing_chats()
        if chat_ids:
            n = self.gate.baseline_many(chat_ids)
            logger.info("baseline existing chats: %s", n)
        self._bg_tasks.append(asyncio.create_task(self._followup_loop()))
        self._bg_tasks.append(asyncio.create_task(self._avito_retry_loop()))

    async def shutdown(self) -> None:
        for t in self._bg_tasks:
            t.cancel()

    def verify_webhook(self, request_headers: dict[str, str], body: bytes) -> bool:
        """Если секрет задан — требуем совпадение заголовка. Без секрета — warning."""
        _ = body
        secret = (self.settings.wazzup_webhook_secret or "").strip()
        if not secret:
            logger.warning("wazzup webhook secret not set — accepting (configure before prod)")
            return True
        provided = (
            request_headers.get("x-wazzup-secret")
            or request_headers.get("x-webhook-secret")
            or request_headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        return bool(provided) and secrets.compare_digest(provided, secret)

    async def handle_webhook_payload(self, payload: dict[str, Any]) -> None:
        messages = self.wazzup.parse_webhook(payload)
        for msg in messages:
            await self.ingest(msg)

    async def ingest(self, msg: IncomingMessage) -> None:
        if msg.message_id and self.db.seen_message(msg.message_id):
            return
        if msg.message_id:
            self.db.mark_seen(msg.message_id, msg.chat_id)

        # Авито show-phone — в MAX, клиенту не отвечаем
        if self.wazzup.looks_like_avito_show_phone(msg):
            await self.avito_job.on_event(msg)
            return

        status = self.gate.status(msg.chat_id)
        if status is None:
            # до ключей Wazzup считаем новое входящее первым контактом
            status = self.gate.classify_first_seen(
                msg.chat_id, had_prior_human_outgoing=False
            )

        if status != BOT_OWNED:
            logger.info("skip chat=%s status=%s", msg.chat_id, status)
            return

        self.db.touch_user_message(msg.chat_id)
        self.db.add_message(
            msg.chat_id,
            role="user",
            text=msg.text or "",
            kind=msg.kind,
            external_id=msg.message_id or None,
        )
        await self.batcher.add(msg)

    async def _on_batch(self, chat_id: str, messages: list[IncomingMessage]) -> None:
        if not self.gate.bot_may_reply(chat_id):
            return
        result = await self.dialog.handle(chat_id, messages)
        if result.markers.price_request:
            history = self.db.recent_messages(chat_id, self.settings.history_limit)
            summary = "\n".join(
                f"{m['role']}: {m['text']}" for m in history if m.get("text")
            )[-2000:]
            await self.price_relay.on_client_wants_price(
                chat_id=chat_id,
                summary=summary or "(нет истории)",
                ask=result.markers.price_request,
            )
        if result.markers.operator:
            self.db.upsert_chat(chat_id, status="manual")
            logger.info("chat=%s → manual (operator marker)", chat_id)
            return
        if not result.reply:
            return
        send = await self.wazzup.send_text(chat_id, result.reply)
        if send.ok:
            self.db.add_message(chat_id, role="assistant", text=result.reply)
            self.db.touch_bot_message(chat_id)

    async def _followup_loop(self) -> None:
        while True:
            try:
                await self.followup_job.tick()
            except Exception:
                logger.exception("followup tick failed")
            await asyncio.sleep(60 if self.settings.fast_mode else 300)

    async def _avito_retry_loop(self) -> None:
        while True:
            try:
                await self.avito_job.tick()
            except Exception:
                logger.exception("avito retry tick failed")
            await asyncio.sleep(120 if self.settings.fast_mode else 600)
