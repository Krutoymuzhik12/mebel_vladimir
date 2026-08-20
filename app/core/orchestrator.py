"""Оркестратор: Wazzup → gatekeeper → batcher → dialog → reply.

Ключи Wazzup/Poe/MAX подключим позже; каркас уже гоняет безопасный путь.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from typing import Any

from app.config import ROOT, Settings
from app.core.batcher import MessageBatcher
from app.core.dialog import DialogService
from app.core.gatekeeper import BOT_OWNED, START_CMD, STOP_CMD, Gatekeeper
from app.core.quiet_hours import QuietHours
from app.db.database import Database
from app.jobs.avito_show_phone import AvitoShowPhoneJob
from app.jobs.document_relay import DocumentRelay
from app.jobs.followups import FollowUpJob
from app.jobs.max_inbox import MaxInbox
from app.jobs.price_relay import PriceRelay
from app.notify.max import MaxNotifier
from app.services import amocrm, backup
from app.services.health_watch import HealthWatch
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
        self.price_relay = PriceRelay(db, wazzup, self.max_notifier, settings)
        self.document_relay = DocumentRelay(db, wazzup, self.max_notifier, settings)
        self.avito_job = AvitoShowPhoneJob(db, wazzup, self.max_notifier, settings)
        self.followup_job = FollowUpJob(
            settings, db, wazzup, self.quiet, self.max_notifier
        )
        self.max_inbox = MaxInbox(
            settings, db, self.max_notifier, self.price_relay, self.document_relay
        )
        self.health = HealthWatch(settings, self.max_notifier)
        self.batcher = MessageBatcher(settings, self._on_batch)
        self._bg_tasks: list[asyncio.Task] = []
        # Загружен ли список старых чатов. Пока нет — незнакомым не пишем.
        self.baseline_ready = False

    async def startup(self) -> None:
        # Baseline existing: файл + (позже) amoCRM API
        try:
            crm_ids = await amocrm.baseline_existing_ids(self.settings)
        except Exception:
            logger.exception("amocrm baseline failed")
            crm_ids = []
        wazzup_ids = await self.wazzup.baseline_existing_chats()
        chat_ids = list(dict.fromkeys([*crm_ids, *wazzup_ids]))
        if chat_ids:
            n = self.gate.baseline_many(chat_ids)
            logger.info("baseline existing chats: помечено %s из %s", n, len(chat_ids))
        else:
            logger.info("baseline existing: пусто (файл/API без ключей)")

        # База считается загруженной, если история пришла сейчас ИЛИ уже лежит
        # с прошлого успешного импорта — иначе перезапуск при недоступной CRM
        # снял бы защиту, хотя все старые чаты в базе на месте.
        minimum = max(1, int(self.settings.baseline_min_chats))
        known_old = self.db.count_chats_by_status("existing")
        self.baseline_ready = len(chat_ids) >= minimum or known_old >= minimum
        if self.baseline_ready:
            logger.info(
                "история известна (%s старых чатов) — незнакомый чат считаем новым",
                known_old,
            )
        else:
            logger.warning(
                "история НЕ загружена (%s старых чатов при пороге %s): незнакомым "
                "чатам бот отвечать не будет. Проверьте AMOCRM_TOKEN.",
                known_old,
                minimum,
            )

        self._bg_tasks.append(asyncio.create_task(self._followup_loop()))
        self._bg_tasks.append(asyncio.create_task(self._avito_retry_loop()))
        self._bg_tasks.append(asyncio.create_task(self.max_inbox.run_forever()))
        if self.settings.backup_enabled:
            self._bg_tasks.append(asyncio.create_task(self._backup_loop()))
        if self.settings.health_watch_enabled:
            self._bg_tasks.append(asyncio.create_task(self._health_loop()))

    async def shutdown(self) -> None:
        for t in self._bg_tasks:
            t.cancel()
        await self.wazzup.aclose()
        await self.max_notifier.aclose()

    def verify_webhook(
        self,
        request_headers: dict[str, str],
        body: bytes,
        path_secret: str | None = None,
    ) -> bool:
        """Проверка секрета.

        Wazzup не умеет слать произвольный заголовок — штатный способ защиты это
        секрет в самом URL вебхука (/webhooks/wazzup/<secret>). Заголовок тоже
        принимаем: удобно для ручных тестов через curl.
        """
        _ = body
        secret = (self.settings.wazzup_webhook_secret or "").strip()
        if not secret:
            logger.warning(
                "WAZZUP_WEBHOOK_SECRET не задан — принимаю без проверки "
                "(задайте перед боевым запуском)"
            )
            return True
        candidates = [
            path_secret or "",
            request_headers.get("x-wazzup-secret", ""),
            request_headers.get("x-webhook-secret", ""),
            request_headers.get("authorization", "").removeprefix("Bearer ").strip(),
        ]
        return any(
            candidate and secrets.compare_digest(candidate, secret)
            for candidate in candidates
        )

    async def handle_webhook_payload(self, payload: dict[str, Any]) -> None:
        # Wazzup проверяет URL тестовым запросом при подписке
        if payload.get("test") is True:
            logger.info("wazzup: проверочный запрос вебхука — ok")
            return
        messages = self.wazzup.parse_webhook(payload)
        if not messages:
            logger.info("wazzup: в payload нет сообщений для обработки")
            return
        for msg in messages:
            try:
                await self.ingest(msg)
            except Exception:
                logger.exception("ingest failed chat=%s", msg.chat_id)

    async def ingest(self, msg: IncomingMessage) -> None:
        # Тестовый режим: слушаем только разрешённые каналы
        if not self.wazzup.channel_allowed(msg.channel_id, msg.channel):
            logger.info(
                "skip (тестовый режим) chat=%s channel_id=%s type=%s",
                msg.chat_id,
                msg.channel_id,
                msg.channel,
            )
            return

        # Тестовый режим: на разрешённом канале может сидеть и настоящий
        # клиент, поэтому дополнительно отсекаем всех, кроме тестировщиков
        if not self.wazzup.chat_allowed(msg.chat_id):
            logger.info(
                "skip (тестовый режим) chat=%s — не в списке TEST_CHAT_IDS",
                msg.chat_id,
            )
            return

        # /start обнуляет чат: следующее сообщение пойдёт как первый контакт.
        # Делаем ДО дедупа — иначе стёртый id сообщения останется в
        # seen_messages и повторный /start молча ничего не сделает.
        if self._is_reset_command(msg):
            wiped = self.db.forget_chat(msg.chat_id)
            logger.info(
                "chat=%s /start — чат забыт: сообщений=%s, заявок=%s",
                msg.chat_id,
                wiped.get("messages", 0),
                wiped.get("price_requests", 0) + wiped.get("document_requests", 0),
            )

        if msg.message_id and self.db.seen_message(msg.message_id):
            return
        if msg.message_id:
            self.db.mark_seen(msg.message_id, msg.chat_id)

        # Судьбу незнакомого чата решаем ДО любой записи в таблицу chats.
        # remember_route ниже делает upsert_chat, а тот создаёт строку со
        # статусом new по умолчанию — и чат становится «известным и нашим»
        # раньше, чем мы успели спросить, наш ли он. Так безопасное умолчание
        # отменялось молча, без единой ошибки в логе.
        # Эхо сюда не пускаем: у нового клиента автоответ площадки приходит
        # тем же эхом, и политика записала бы его в старые.
        status = self.gate.status(msg.chat_id)
        if status is None and not msg.is_echo:
            status = self.gate.on_unknown_chat(
                msg.chat_id,
                channel_id=msg.channel_id,
                fresh_channels=self.settings.fresh_channel_id_set,
                policy=self.settings.first_contact_policy,
                baseline_ready=self.baseline_ready,
            )

        # Запоминаем, куда отвечать: без channelId+chatType Wazzup ответ не примет
        self.db.remember_route(
            msg.chat_id, channel_id=msg.channel_id, chat_type=msg.channel
        )

        # Исходящее не через наш API — менеджер написал руками / #стоп / #старт
        if msg.is_echo:
            await self._on_staff_echo(msg)
            return

        # Стикеры и видео: не отвечаем и в диалог не кладём
        if msg.kind in {"sticker", "video"}:
            logger.info("skip chat=%s kind=%s — не реагируем", msg.chat_id, msg.kind)
            return

        # Авито show-phone — в MAX, клиенту не отвечаем
        if self.wazzup.looks_like_avito_show_phone(msg):
            await self.avito_job.on_event(msg)
            return

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

        # Документ (PDF/Word/др.): содержимое не разбираем, файл целиком уходит
        # владельцу через MAX — так же, как цена, но без модели в цепочке.
        if msg.kind == "document":
            await self._on_document(msg)
            return

        await self.batcher.add(msg)

    async def _on_document(self, msg: IncomingMessage) -> None:
        request_id = await self.document_relay.on_client_sent_document(
            chat_id=msg.chat_id, doc_url=msg.media_url or "", note=msg.text or ""
        )
        if request_id:
            ack = (
                "Файл получил, передал специалисту — он посмотрит и ответит "
                "вам здесь."
            )
            logger.info(
                "document forwarded chat=%s request_id=%s", msg.chat_id, request_id
            )
        else:
            ack = (
                "Файл получил, но сейчас не могу передать его специалисту. "
                "Опишите, пожалуйста, коротко словами, что в файле, и я передам "
                "менеджеру."
            )
            logger.warning("document forward failed chat=%s", msg.chat_id)

        send = await self.wazzup.send_text(msg.chat_id, ack)
        if send.ok:
            self.db.add_message(
                msg.chat_id,
                role="assistant",
                text=ack,
                external_id=send.external_id or None,
            )
            if send.external_id:
                self.db.mark_seen(send.external_id, msg.chat_id)
            self.db.touch_bot_message(msg.chat_id)

    def _is_reset_command(self, msg: IncomingMessage) -> bool:
        """Клиент нажал /start и мы в тестовом режиме.

        Только тест: в бою человек, набравший /start, не должен терять
        историю переписки — он этого не ожидает и не поймёт, почему бот
        вдруг спрашивает то, что уже знал.
        """
        if msg.is_echo or not self.settings.test_mode:
            return False
        if not self.settings.test_reset_on_start:
            return False
        return (msg.text or "").strip().lower() in {"/start", "/старт", "/reset"}

    async def _on_staff_echo(self, msg: IncomingMessage) -> None:
        """Исходящее, отправленное не через наш API.

        Wazzup помечает isEcho и для наших сообщений в некоторых каналах, поэтому
        сначала отсекаем собственные реплики по тексту — иначе бот сам себя
        переведёт в manual и замолчит.
        """
        text = (msg.text or "").strip()
        if not text:
            return

        recent = self.db.recent_messages(chat_id=msg.chat_id, limit=10)
        own = {
            (r.get("text") or "").strip()
            for r in recent
            if r.get("role") == "assistant" and r.get("text")
        }
        if text in own:
            logger.debug("echo нашего же сообщения chat=%s — игнор", msg.chat_id)
            return

        normalized = text.lower()
        is_command = normalized in {STOP_CMD, START_CMD}

        # Автоответ Wazzup («Спасибо, что написали, мы скоро ответим») приходит
        # тем же эхом, что и ручная реплика менеджера, но без автора. У живого
        # сотрудника authorName заполнен всегда. Без этой проверки первое же
        # приветствие площадки уводило чат в manual, и бот замолкал навсегда.
        if (
            not is_command
            and self.settings.staff_takeover_requires_author
            and not (msg.author_name or "").strip()
        ):
            logger.info(
                "эхо без автора chat=%s — автоответ площадки, не перехват | %s",
                msg.chat_id,
                text[:120],
            )
            return

        if not is_command and not self.settings.staff_takeover_on_echo:
            logger.info(
                "staff echo chat=%s — перехват выключен (STAFF_TAKEOVER_ON_ECHO=0)",
                msg.chat_id,
            )
            return

        new_status = self.gate.on_staff_message(msg.chat_id, text)
        logger.info(
            "staff message chat=%s author=%s → status=%s",
            msg.chat_id,
            msg.author_name or "?",
            new_status,
        )

    async def _on_batch(self, chat_id: str, messages: list[IncomingMessage]) -> None:
        if not self.gate.bot_may_reply(chat_id):
            return
        result = await self.dialog.handle(chat_id, messages)
        if result.extracted:
            self.db.merge_facts(chat_id, result.extracted)
        # Интент нужен и после диалога: по нему выбираем, чем дожимать (stall.py)
        if result.intent:
            self.db.upsert_chat(chat_id, last_intent=result.intent)
        if result.wants_price or result.markers.price_request:
            history = self.db.recent_messages(chat_id, self.settings.history_limit)
            summary = "\n".join(
                f"{m['role']}: {m['text']}" for m in history if m.get("text")
            )[-2000:]
            ask = result.markers.price_request or (
                result.extracted.get("furniture")
                and f"расчёт: {result.extracted.get('furniture')}"
            ) or "клиент спрашивает стоимость"
            await self.price_relay.on_client_wants_price(
                chat_id=chat_id,
                summary=summary or "(нет истории)",
                ask=str(ask),
            )
        # Клиент попросил вернуться в конкретный срок — маркер от менеджер-бота
        if result.markers.snooze_days:
            due = self.db.snooze_chat(
                chat_id,
                result.markers.snooze_days,
                result.markers.snooze_reason or "",
            )
            logger.info(
                "chat=%s отложен на %s дн. до %s (%s)",
                chat_id,
                result.markers.snooze_days,
                due[:10],
                result.markers.snooze_reason or "без причины",
            )

        if result.markers.operator:
            self.db.upsert_chat(chat_id, status="manual")
            logger.info("chat=%s → manual (operator marker)", chat_id)
            return
        if not result.reply:
            return

        await self._human_pause()
        send = await self.wazzup.send_text(chat_id, result.reply)
        if send.ok:
            self.db.add_message(
                chat_id,
                role="assistant",
                text=result.reply,
                external_id=send.external_id or None,
            )
            # свой же message_id — чтобы эхо не приняли за ответ менеджера
            if send.external_id:
                self.db.mark_seen(send.external_id, chat_id)
            self.db.touch_bot_message(chat_id)
            await self._send_catalog_photos(chat_id, result.matches)
        else:
            logger.error("reply not sent chat=%s err=%s", chat_id, send.error)

    async def _send_catalog_photos(
        self, chat_id: str, matches: list[dict[str, Any]]
    ) -> None:
        """Фото найденных по картинке позиций — вслед за текстом ответа."""
        if not matches or not self.settings.send_catalog_photos:
            return
        for item in matches[: max(1, self.settings.catalog_photos_limit)]:
            # Новый каталог хранит прямые ссылки на фото — Wazzup заберёт их
            # сам. Путь на диске остаётся запасным путём для старой выгрузки.
            photos = item.get("photos") or []
            url = str(photos[0]) if photos else self.settings.public_media_url(
                str(item.get("photo_path") or "")
            )
            if not url:
                logger.info("фото каталога не отправлено: нет ссылки")
                return
            caption = self._photo_caption(item)
            send = await self.wazzup.send_media(chat_id, url, caption=caption)
            if send.ok:
                self.db.add_message(chat_id, role="assistant", text=caption, kind="image")
                if send.external_id:
                    self.db.mark_seen(send.external_id, chat_id)
            else:
                logger.info(
                    "фото каталога не ушло chat=%s url=%s err=%s",
                    chat_id,
                    url,
                    send.error,
                )

    @staticmethod
    def _photo_caption(item: dict[str, Any]) -> str:
        """Подпись под фото.

        Название модели клиенту ничего не говорит: «Дерби» и «Трансформер» —
        внутренние имена. Работает картинка, а подпись должна отвечать на то,
        что человек спросил бы следующим сообщением: сколько стоит и какое оно.
        """
        parts: list[str] = []
        name = str(item.get("name") or item.get("article") or "").strip()
        if name:
            parts.append(name)

        price = item.get("price")
        price_text = str(item.get("price_text") or "").strip()
        if isinstance(price, int) and price > 0:
            parts.append(f"от {price:,} руб.".replace(",", " "))
        elif price_text:
            parts.append(f"от {price_text}")

        details: list[str] = []
        # Только то, что клиент назвал бы сам: форма, цвет, спальное место
        for feature in (item.get("features") or [])[:2]:
            details.append(str(feature))
        colors = [str(c) for c in (item.get("colors") or [])[:2]]
        if colors:
            details.append("/".join(colors))
        sizes = item.get("sizes") or []
        if sizes:
            details.append(str(sizes[0]))
        if details:
            parts.append(", ".join(details))

        return " — ".join(parts)

    async def _human_pause(self) -> None:
        """Пауза перед ответом — чтобы бот не отвечал мгновенно."""
        if self.settings.fast_mode:
            return
        low = max(0.0, self.settings.reply_delay_min_sec)
        high = max(low, self.settings.reply_delay_max_sec)
        if high <= 0:
            return
        await asyncio.sleep(random.uniform(low, high))

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

    async def _backup_loop(self) -> None:
        # Первый бэкап почти сразу после старта, дальше — по интервалу
        await asyncio.sleep(30 if self.settings.fast_mode else 60)
        while True:
            try:
                dest_dir = ROOT / self.settings.backup_dir
                backup.backup_db(
                    self.settings.db_file,
                    dest_dir,
                    keep=max(1, int(self.settings.backup_keep)),
                )
            except Exception:
                logger.exception("backup failed")
            hours = max(0.1, float(self.settings.backup_interval_hours))
            await asyncio.sleep(60 if self.settings.fast_mode else hours * 3600)

    async def _health_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.health.check()
            except Exception:
                logger.exception("health watch failed")
            wait = max(30.0, float(self.settings.health_watch_interval_sec))
            await asyncio.sleep(60 if self.settings.fast_mode else wait)
