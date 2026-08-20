"""Единый интерфейс канала. Оркестратор не знает про Wazzup/Avito напрямую."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class IncomingMessage:
    """Нормализованное входящее из любого канала."""

    chat_id: str
    message_id: str
    text: str = ""
    # image | voice | text | video | document | sticker | geo | system
    kind: str = "text"
    channel: str = ""  # whatsapp, avito, telegram, ...
    # UUID канала Wazzup — обязателен, чтобы отправить ответ обратно
    channel_id: str = ""
    # Исходящее, отправленное НЕ через наш API (менеджер написал руками)
    is_echo: bool = False
    # Имя автора исходящего (сотрудник) / контакта
    author_name: str = ""
    contact_name: str = ""
    # Системное событие площадки (например Авито «показ номера»)
    is_system: bool = False
    # Сырой payload — для отладки и уточнения форматов
    raw: dict[str, Any] = field(default_factory=dict)
    media_url: str | None = None
    ts: float | None = None


@dataclass
class SendResult:
    ok: bool
    external_id: str = ""
    error: str = ""


class Transport(Protocol):
    name: str

    async def send_text(self, chat_id: str, text: str) -> SendResult: ...

    async def send_media(
        self, chat_id: str, file_path: str, caption: str = ""
    ) -> SendResult: ...
