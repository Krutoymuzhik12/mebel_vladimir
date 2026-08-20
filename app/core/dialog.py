"""Диалог: Intent (Vladimir_Intent) + Manager (Vladimir_dialog) через Poe."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.catalog import search as catalog_search
from app.catalog import vocab
from app.config import settings
from app.core.markers import Markers, extract
from app.db.database import Database
from app.services import attachments, media, transcription
from app.services.poe import PoeClient
from app.transports.base import IncomingMessage
from app.vision.client import VisionClient

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
    # Похожие позиции каталога, если клиент прислал фото
    matches: list[dict[str, Any]] = field(default_factory=list)


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
        self.vision = VisionClient()
        self.catalog = catalog_search.shared()

    async def _read_attachments(
        self, messages: list[IncomingMessage]
    ) -> tuple[str, list[str]]:
        """Документ / geo → подсказки + текст из PDF/DOCX.

        Стикеры и видео сюда не доходят — orchestrator их отбрасывает.
        """
        texts: list[str] = []
        hints: list[str] = []
        for m in messages:
            if m.kind not in {"document", "geo", "unsupported"}:
                continue
            hint = attachments.hint_for(m.kind)
            if hint:
                hints.append(hint)
            if m.kind == "document" and m.media_url:
                extracted = await attachments.extract_from_url(m.media_url)
                if extracted:
                    logger.info(
                        "документ разобран chat=%s chars=%s", m.chat_id, len(extracted)
                    )
                    texts.append(f"(текст из документа клиента)\n{extracted}")
                    if self.db is not None and m.message_id:
                        self.db.set_message_text(
                            m.message_id, extracted[:2000]
                        )
                else:
                    hints.append(
                        "Из документа текст не извлечён (скан/неподдерживаемый "
                        "формат). Не выдумывай содержимое файла."
                    )
        return "\n".join(texts).strip(), hints

    async def _read_voice(
        self, messages: list[IncomingMessage]
    ) -> tuple[str, list[str]]:
        """Голосовые → текст.

        Расшифровку кладём в историю вместо пустой строки: иначе следующий
        вопрос клиента модель будет читать без того, что он наговорил.
        """
        texts: list[str] = []
        hints: list[str] = []
        for m in messages:
            if m.kind != "voice" or not m.media_url:
                continue
            raw = await media.download(
                m.media_url, max_bytes=settings.voice_max_bytes
            )
            if raw is None:
                hints.append(
                    "Клиент прислал голосовое, но файл не скачался или слишком "
                    "длинный. Извинись и попроси написать текстом или короткое "
                    "голосовое до полутора минут."
                )
                continue
            if len(raw) > settings.voice_max_bytes:
                hints.append(
                    "Голосовое слишком длинное. Попроси коротко текстом или "
                    "голосовое до полутора минут."
                )
                continue
            text = await transcription.transcribe_bytes(
                raw, suffix=media.suffix_for(m.media_url, "voice")
            )
            if text.startswith("[голосовое слишком длинное]"):
                hints.append(
                    "Голосовое слишком длинное. Попроси коротко текстом или "
                    "голосовое до полутора минут."
                )
                continue
            if transcription.unclear(text):
                logger.info("голосовое не разобрано chat=%s: %r", m.chat_id, text[:80])
                hints.append(
                    "Клиент прислал голосовое, но разобрать речь не вышло. "
                    "Скажи, что не расслышал, и попроси повторить — коротко, "
                    "без формальностей."
                )
                continue
            logger.info("голосовое расшифровано chat=%s: %s", m.chat_id, text[:120])
            texts.append(text)
            if self.db is not None and m.message_id:
                self.db.set_message_text(m.message_id, text)
        if texts:
            # Клиент ждёт ответа по существу, а не подтверждения, что мы поняли
            hints.append(
                "Текст клиента получен из голосового сообщения. Отвечай сразу по "
                "сути. Не пересказывай услышанное и не спрашивай «правильно ли я "
                "понял» — клиент знает, что он сказал."
            )
        return "\n".join(texts).strip(), hints

    async def _read_photos(
        self, messages: list[IncomingMessage], furniture: str = ""
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Фото клиента → похожие позиции каталога.

        furniture — что за мебель обсуждают. Сужает поиск до нужного типа:
        иначе фото столешницы найдёт белый диван, и это будет выглядеть
        так, будто бот не смотрел на картинку.
        """
        wanted_type = vocab.type_from_client_words(furniture)
        photos = [m for m in messages if m.kind == "image" and m.media_url]
        if not photos:
            return [], []
        if not self.vision.enabled:
            return [
                "Клиент прислал фото. Подбор по картинке сейчас недоступен — "
                "опиши словами, что уточнить, и предложи подобрать вариант."
            ], []

        matches: list[dict[str, Any]] = []
        for m in photos:
            raw = await media.download(m.media_url)
            if raw is None:
                continue
            try:
                found = await self.vision.search_bytes(
                    raw,
                    filename=f"photo{media.suffix_for(m.media_url, 'image')}",
                    top_k=settings.vision_top_k,
                    types=wanted_type or None,
                )
            except Exception:
                logger.exception("vision: поиск не удался chat=%s", m.chat_id)
                continue
            matches.extend(found.get("matches") or [])

        if not matches:
            return [
                "Клиент прислал фото, но похожего в каталоге не нашлось. Не "
                "выдумывай модель: скажи, что такого в наличии нет, и уточни "
                "размеры и материал, чтобы предложить аналог под заказ."
            ], []

        # один и тот же артикул мог прийти с нескольких фото
        unique: dict[str, dict[str, Any]] = {}
        for item in matches:
            art = item.get("article")
            if art and art not in unique:
                unique[art] = item
        best = sorted(
            unique.values(), key=lambda i: float(i.get("similarity") or 0), reverse=True
        )[: settings.vision_top_k]

        lines = "\n".join(
            f"- {i.get('name') or i.get('article')} (артикул {i.get('article')}, "
            f"{i.get('price') or 'цену уточнить'}, схожесть "
            f"{float(i.get('similarity') or 0):.2f})"
            for i in best
        )
        logger.info("vision: найдено %s позиций", len(best))
        return [
            "Клиент прислал фото. В нашем каталоге похожи:\n"
            f"{lines}\n"
            "Назови подходящие модели и цену из этого списка — ничего к нему не "
            "добавляй. Фото этих позиций клиенту отправит система, описывать "
            "картинку словами не нужно."
        ], best

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

    def _pick_from_catalog(
        self,
        user_text: str,
        intent: str | None,
        known: dict[str, Any],
        extracted: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Три подходящие позиции — или ничего.

        Показываем не всегда: подборка уместна, когда клиент просит примеры
        или обсуждает конкретную мебель. Вываливать каталог в ответ на
        «здравствуйте» — верный способ выглядеть рассылкой.
        """
        if not self.catalog.loaded:
            return []
        if intent not in {"catalog_request", "product_question", "price_question",
                          "qualification_answer"}:
            return []

        furniture = str(extracted.get("furniture") or known.get("furniture") or "")
        # Тип обязателен: без него «покажите фото» вернёт случайные позиции
        if not (furniture or vocab.type_from_client_words(user_text)):
            return []

        budget_raw = str(extracted.get("budget") or known.get("budget") or "")
        digits = "".join(ch for ch in budget_raw if ch.isdigit())
        budget = int(digits) if digits else 0
        if 0 < budget < 1000:  # «до 150 тысяч» → 150
            budget *= 1000

        style = str(extracted.get("style") or known.get("style") or "")
        found = self.catalog.search(
            f"{user_text} {style}",
            furniture=furniture,
            colors=tuple(vocab.detect_colors(style)),
            budget=budget,
            limit=3,
        )
        if found:
            logger.info(
                "каталог: подобрано %s под intent=%s furniture=%r",
                len(found), intent, furniture or user_text[:30],
            )
        return found

    @staticmethod
    def _catalog_hint(items: list[dict[str, Any]]) -> str:
        lines = []
        for i in items:
            bits = [i.get("name") or i.get("article")]
            if i.get("price"):
                bits.append(f"от {i['price']:,} руб.".replace(",", " "))
            if i.get("colors"):
                bits.append("цвета: " + ", ".join(i["colors"][:3]))
            if i.get("sizes"):
                bits.append("размер " + i["sizes"][0])
            lines.append("- " + "; ".join(str(b) for b in bits if b))
        body = "\n".join(lines)
        return (
            "Система сейчас отправит клиенту фото этих позиций:\n"
            f"{body}\n"
            "Назови их коротко своими словами и спроси, что ближе. "
            "Не выдумывай других моделей, цен и характеристик, кроме этих. "
            "Фотографии описывать словами не нужно."
        )

    async def handle(
        self, chat_id: str, messages: list[IncomingMessage]
    ) -> DialogResult:
        user_text = "\n".join(m.text for m in messages if m.text).strip()
        hints: list[str] = []
        # Факты нужны раньше обычного: по ним сужаем поиск по фото до нужного
        # типа мебели ещё до того, как классификатор скажет своё слово
        known: dict[str, Any] = self._known_from_db(chat_id)

        voice_text, voice_hints = await self._read_voice(messages)
        hints += voice_hints
        if voice_text:
            user_text = f"{user_text}\n{voice_text}".strip()

        att_text, att_hints = await self._read_attachments(messages)
        hints += att_hints
        if att_text:
            user_text = f"{user_text}\n{att_text}".strip()

        photo_hints, matches = await self._read_photos(
            messages, furniture=f"{known.get('furniture') or ''} {user_text}"
        )
        hints += photo_hints

        # Медиа без подписи: не отдаём в модель пустую строку
        if not user_text:
            kinds = {m.kind for m in messages}
            if "image" in kinds:
                user_text = "(клиент прислал фото)"
            elif "voice" in kinds:
                user_text = "(клиент прислал голосовое)"
            elif "document" in kinds:
                user_text = "(клиент прислал документ)"
            elif "geo" in kinds:
                user_text = "(клиент прислал геолокацию)"
            elif "unsupported" in kinds:
                user_text = "(клиент прислал вложение)"
        history_rows = (
            self.db.recent_messages(chat_id, self.history_limit) if self.db else []
        )
        history = [
            {"role": r["role"], "content": r["text"]}
            for r in history_rows
            if r.get("text") and r.get("role") in {"user", "assistant"}
        ]
        # текущий батч уже в БД как user — history его содержит

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

        # Подборка из каталога. Фото клиента уже дало совпадения — тогда
        # текстовый поиск не нужен, картинка точнее любых слов.
        if not matches:
            picked = self._pick_from_catalog(user_text, intent_name, known, extracted)
            if picked:
                matches = picked
                hints.append(self._catalog_hint(picked))

        mode = "продолжение"
        if len(history_rows) <= max(1, len(messages)):
            mode = "первое обращение"

        if wants_price:
            hints.append(
                "Клиент ждёт цену (intent price_question). "
                "Если точного ориентира мало — поставь [[РАСЧЁТ: ...]]."
            )

        if not self.poe.enabled:
            logger.warning("dialog: POE_API_KEY нет — fallback")
            return DialogResult(reply=FALLBACK, markers=Markers(), matches=matches)

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
            matches=matches,
        )
