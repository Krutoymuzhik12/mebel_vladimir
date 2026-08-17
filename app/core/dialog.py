"""Диалог: Intent (Vladimir_Intent) + Manager (Vladimir_dialog) через Poe."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import settings
from app.core.markers import Markers, extract
from app.db.database import Database
from app.services.poe import PoeClient
from app.transports.base import IncomingMessage

logger = logging.getLogger(__name__)

FALLBACK = "Секунду, уточню этот момент и вернусь с ответом."

_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_FIELD_TITLES = {
    "name": "имя",
    "phone": "телефон",
    "furniture": "что за мебель",
    "room": "помещение",
    "size": "размеры / метры по стене",
    "material": "материал фасадов",
    "style": "стиль и цвет",
    "budget": "бюджет",
    "timing": "сроки",
    "address": "адрес объекта",
}


@dataclass
class DialogResult:
    reply: str
    markers: Markers
    intent: str | None = None
    confidence: float = 0.0
    extracted: dict[str, Any] = field(default_factory=dict)
    wants_price: bool = False


def human_date(day: date) -> str:
    return f"{day.day}.{day.month:02d} ({_WEEKDAYS[day.weekday()]})"


class DialogService:
    def __init__(
        self,
        db: Database | None = None,
        poe: PoeClient | None = None,
    ) -> None:
        self.history_limit = settings.history_limit
        self.db = db
        self.poe = poe or PoeClient()

    def _known_from_db(self, chat_id: str) -> dict[str, Any]:
        """Факты, накопленные из extracted за прошлые сообщения (chats.facts)."""
        if self.db is None:
            return {}
        try:
            return self.db.get_facts(chat_id)
        except Exception:
            logger.exception("не удалось прочитать факты chat=%s", chat_id)
            return {}

    def _context_block(
        self,
        *,
        mode: str,
        answers: dict[str, Any],
        message: str,
        hints: list[str] | None = None,
    ) -> str:
        known_lines = [
            f"{_FIELD_TITLES.get(k, k)}: {v}" for k, v in (answers or {}).items() if v
        ]
        known = "\n".join(known_lines) or "пока ничего не известно"
        phone = bool(answers.get("phone"))
        goal = (
            "ЦЕЛЬ РАЗГОВОРА: довести до расчёта/следующего шага. Телефон уже есть."
            if phone
            else "ЦЕЛЬ РАЗГОВОРА: получить номер телефона и имя."
        )
        hints_text = "\n".join(f"- {h}" for h in (hints or [])) or "- нет"
        return f"""[СЛУЖЕБНЫЙ БЛОК - клиент его не писал и не увидит]
РЕЖИМ: {mode}
Сегодня {human_date(date.today())}.
ТЕБЯ ЗОВУТ {settings.manager_name}. Компания: {settings.company_name}.

Что известно о клиенте и заказе:
{known}

{goal}

Подсказки системы:
{hints_text}

Сообщение клиента:
{message}
"""

    async def handle(
        self, chat_id: str, messages: list[IncomingMessage]
    ) -> DialogResult:
        user_text = "\n".join(m.text for m in messages if m.text).strip()
        # Фото/голос без подписи: не отдаём в модель пустую строку
        if not user_text:
            kinds = {m.kind for m in messages}
            if "image" in kinds:
                user_text = "(клиент прислал фото)"
            elif "voice" in kinds:
                user_text = "(клиент прислал голосовое)"
        history_rows = (
            self.db.recent_messages(chat_id, self.history_limit) if self.db else []
        )
        history = [
            {"role": r["role"], "content": r["text"]}
            for r in history_rows
            if r.get("text") and r.get("role") in {"user", "assistant"}
        ]
        # текущий батч уже в БД как user — history его содержит

        known: dict[str, Any] = self._known_from_db(chat_id)
        intent_name: str | None = None
        confidence = 0.0
        extracted: dict[str, Any] = {}
        wants_price = False

        if self.poe.enabled and user_text:
            try:
                classified = await self.poe.classify(history, user_text, known)
            except Exception:
                logger.exception("classify failed chat=%s", chat_id)
                classified = None
            if classified:
                intent_name = classified.get("intent")
                confidence = float(classified.get("confidence") or 0)
                extracted = classified.get("extracted") or {}
                wants_price = intent_name in {
                    "price_question",
                    "price_inquiry",
                    "waiting_for_price",
                }
                logger.info(
                    "intent chat=%s intent=%s conf=%.2f price=%s",
                    chat_id,
                    intent_name,
                    confidence,
                    wants_price,
                )

        mode = "продолжение"
        if len(history_rows) <= max(1, len(messages)):
            mode = "первое обращение"

        hints: list[str] = []
        if wants_price:
            hints.append(
                "Клиент ждёт цену (intent price_question). "
                "Если точного ориентира мало — поставь [[РАСЧЁТ: ...]]."
            )

        if not self.poe.enabled:
            logger.warning("dialog: POE_API_KEY нет — fallback")
            return DialogResult(reply=FALLBACK, markers=Markers())

        try:
            raw = await self.poe.manager_reply(
                history,
                self._context_block(
                    mode=mode,
                    answers={**known, **{k: v for k, v in extracted.items() if v}},
                    message=user_text,
                    hints=hints,
                ),
            )
        except Exception:
            logger.exception("manager_reply failed chat=%s", chat_id)
            return DialogResult(
                reply=FALLBACK,
                markers=Markers(),
                intent=intent_name,
                confidence=confidence,
                extracted=extracted,
                wants_price=wants_price,
            )

        clean, markers = extract(raw or "")
        if markers.price_request:
            wants_price = True
        return DialogResult(
            reply=clean,
            markers=markers,
            intent=intent_name,
            confidence=confidence,
            extracted=extracted,
            wants_price=wants_price,
        )
