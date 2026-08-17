"""Адаптер Wazzup API v3 — основной I/O чатов.

Вход:  webhook POST от Wazzup (см. parse_webhook).
Выход: POST /v3/message.

Ответ всегда уходит в ту же связку channelId + chatType + chatId, которая
пришла во входящем — так мы не гадаем, как Wazzup называет типы каналов.

Системные события Авито («показ номера» без звонка) приходят сюда же.
amoCRM в коде не участвует.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.transports.base import IncomingMessage, SendResult

logger = logging.getLogger(__name__)

# Типы сообщений Wazzup, которые для нас — картинка
IMAGE_TYPES = {"image", "photo"}
VOICE_TYPES = {"audio", "voice", "ptt"}


class WazzupTransport:
    name = "wazzup"

    def __init__(self, settings: Settings, db: Any = None) -> None:
        self.settings = settings
        self.db = db
        self.base = settings.wazzup_api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    # ---------- инфраструктура ----------

    @property
    def configured(self) -> bool:
        return bool(self.settings.wazzup_api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.wazzup_api_key}",
            "Content-Type": "application/json",
        }

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ---------- тестовый режим ----------

    def channel_allowed(self, channel_id: str, chat_type: str) -> bool:
        """В тестовом режиме слушаем только разрешённые каналы."""
        if not self.settings.test_mode:
            return True
        ids = self.settings.test_channel_id_set
        types = self.settings.test_chat_type_set
        if not ids and not types:
            logger.warning(
                "TEST_MODE=1, но списки TEST_CHANNEL_IDS/TEST_CHAT_TYPES пусты "
                "— игнорирую все каналы"
            )
            return False
        if ids and (channel_id or "").lower() in ids:
            return True
        if types and (chat_type or "").lower() in types:
            return True
        return False

    # ---------- разбор webhook ----------

    def parse_webhook(self, payload: dict[str, Any]) -> list[IncomingMessage]:
        """Разбор webhook Wazzup → нормализованные сообщения.

        Формат: {"messages": [{messageId, channelId, chatType, chatId, type,
                               text, contentUri, isEcho, status, contact}, ...]}
        Ключи statuses/contacts/channelsUpdates игнорируем.
        """
        if self.settings.log_raw_webhook:
            logger.info("wazzup raw payload: %s", payload)

        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            return []

        out: list[IncomingMessage] = []
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            msg = self._to_incoming(item)
            if msg is not None:
                out.append(msg)
        return out

    def _to_incoming(self, item: dict[str, Any]) -> IncomingMessage | None:
        chat_id = str(item.get("chatId") or "").strip()
        if not chat_id:
            logger.info("wazzup: сообщение без chatId — пропуск")
            return None

        status = str(item.get("status") or "").lower()
        is_echo = bool(item.get("isEcho"))
        msg_type = str(item.get("type") or "text").lower()

        # Исходящие, отправленные нами через API, возвращаются со статусами
        # sent/delivered/read и isEcho=false — их обрабатывать не нужно.
        if status and status != "inbound" and not is_echo:
            return None

        contact = item.get("contact")
        contact_name = ""
        if isinstance(contact, dict):
            contact_name = str(contact.get("name") or "")

        if msg_type in IMAGE_TYPES:
            kind = "image"
        elif msg_type in VOICE_TYPES:
            kind = "voice"
        elif msg_type == "text":
            kind = "text"
        else:
            kind = "system"

        chat_type = str(item.get("chatType") or "").lower()
        return IncomingMessage(
            chat_id=chat_id,
            message_id=str(item.get("messageId") or ""),
            text=str(item.get("text") or ""),
            kind=kind,
            channel=chat_type,
            channel_id=str(item.get("channelId") or ""),
            is_echo=is_echo,
            author_name=str(item.get("authorName") or ""),
            contact_name=contact_name,
            is_system=kind == "system",
            raw=item,
            media_url=item.get("contentUri") or None,
        )

    def looks_like_avito_show_phone(self, msg: IncomingMessage) -> bool:
        """Системное событие Авито «клиент показал номер».

        Нужны: канал avito + is_system + маркеры в тексте.
        """
        channel = (msg.channel or "").lower()
        if "avito" not in channel or not msg.is_system:
            return False
        text = (msg.text or "").lower()
        needles = (
            "показ номера",
            "показал номер",
            "показала номер",
            "show phone",
            "просмотр номера",
        )
        return any(n in text for n in needles)

    # ---------- отправка ----------

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        channel_id: str = "",
        chat_type: str = "",
    ) -> SendResult:
        return await self._send(
            chat_id, channel_id=channel_id, chat_type=chat_type, text=text
        )

    async def send_media(
        self,
        chat_id: str,
        file_path: str,
        caption: str = "",
        *,
        channel_id: str = "",
        chat_type: str = "",
    ) -> SendResult:
        """file_path здесь — публичный URL файла (Wazzup принимает contentUri)."""
        return await self._send(
            chat_id,
            channel_id=channel_id,
            chat_type=chat_type,
            text=caption,
            content_uri=file_path,
        )

    def _resolve_route(
        self, chat_id: str, channel_id: str, chat_type: str
    ) -> tuple[str, str]:
        if channel_id and chat_type:
            return channel_id, chat_type
        if self.db is not None:
            stored_id, stored_type = self.db.get_route(chat_id)
            return channel_id or stored_id, chat_type or stored_type
        return channel_id, chat_type

    async def _send(
        self,
        chat_id: str,
        *,
        channel_id: str,
        chat_type: str,
        text: str,
        content_uri: str | None = None,
    ) -> SendResult:
        if not self.configured:
            logger.warning("wazzup send skipped (нет WAZZUP_API_KEY) chat=%s", chat_id)
            return SendResult(ok=False, error="wazzup not configured")

        channel_id, chat_type = self._resolve_route(chat_id, channel_id, chat_type)
        if not channel_id or not chat_type:
            logger.error(
                "wazzup send: нет роутинга chat=%s channel_id=%r chat_type=%r",
                chat_id,
                channel_id,
                chat_type,
            )
            return SendResult(ok=False, error="no channel route for chat")

        if not self.settings.wazzup_send_enabled:
            logger.info(
                "DRY-RUN: не отправляю chat=%s type=%s len=%s | %s",
                chat_id,
                chat_type,
                len(text or ""),
                (text or "")[:200],
            )
            return SendResult(ok=True, external_id="dry-run")

        body: dict[str, Any] = {
            "channelId": channel_id,
            "chatId": chat_id,
            "chatType": chat_type,
        }
        if content_uri:
            body["contentUri"] = content_uri
        if text:
            body["text"] = text
        if not content_uri and not text:
            return SendResult(ok=False, error="empty message")

        try:
            client = await self.client()
            resp = await client.post(
                f"{self.base}/v3/message", json=body, headers=self._headers()
            )
        except httpx.HTTPError as exc:
            logger.exception("wazzup send network error chat=%s", chat_id)
            return SendResult(ok=False, error=f"network: {exc}")

        if resp.status_code in (200, 201):
            data = self._json(resp)
            external_id = str(data.get("messageId") or "") if data else ""
            logger.info(
                "wazzup sent chat=%s type=%s message_id=%s", chat_id, chat_type, external_id
            )
            return SendResult(ok=True, external_id=external_id)

        error = self._describe_error(resp)
        logger.error(
            "wazzup send failed chat=%s http=%s %s", chat_id, resp.status_code, error
        )
        return SendResult(ok=False, error=error)

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any] | None:
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _describe_error(self, resp: httpx.Response) -> str:
        if resp.status_code == 401:
            return "401: неверный WAZZUP_API_KEY"
        if resp.status_code == 403:
            return "403: у ключа нет доступа к каналу"
        if resp.status_code == 429:
            return "429: превышен лимит запросов"
        data = self._json(resp)
        if data:
            err = data.get("error") or data.get("errors") or data
            return f"{resp.status_code}: {err}"
        return f"{resp.status_code}: {resp.text[:300]}"

    # ---------- сервисные вызовы ----------

    async def list_channels(self) -> list[dict[str, Any]]:
        """GET /v3/channels — узнать channelId каждого подключённого канала."""
        if not self.configured:
            raise RuntimeError("WAZZUP_API_KEY не задан в .env")
        client = await self.client()
        resp = await client.get(f"{self.base}/v3/channels", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(self._describe_error(resp))
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    async def set_webhook(self, uri: str) -> dict[str, Any]:
        """PATCH /v3/webhooks — подписаться на входящие сообщения.

        Wazzup сразу дёргает uri тестовым запросом: сервис уже должен отвечать 200.
        """
        if not self.configured:
            raise RuntimeError("WAZZUP_API_KEY не задан в .env")
        body = {
            "webhooksUri": uri,
            "subscriptions": {
                "messagesAndStatuses": True,
                "contactsAndDealsCreation": False,
                "channelsUpdates": False,
            },
        }
        client = await self.client()
        resp = await client.patch(
            f"{self.base}/v3/webhooks", json=body, headers=self._headers()
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(self._describe_error(resp))
        return self._json(resp) or {"ok": True}

    async def get_webhook(self) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("WAZZUP_API_KEY не задан в .env")
        client = await self.client()
        resp = await client.get(f"{self.base}/v3/webhooks", headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(self._describe_error(resp))
        return self._json(resp) or {}

    async def baseline_existing_chats(self) -> list[str]:
        """Список chat_id до старта бота → status=existing.

        Wazzup v3 не отдаёт список чатов через API, поэтому база строится
        по факту: первое входящее в незнакомый чат считается первым контактом.
        """
        return []
