"""Прогон конвейера без сервера и без реального Wazzup.

Проверяет: конфиг → разбор вебхука → фильтр тестового режима → gatekeeper →
батчер → диалог (Poe) → отправка (перехвачена, наружу ничего не уходит).

    python -m scripts.selftest              # только разбор + фильтры, без Poe
    python -m scripts.selftest --with-poe   # дёрнуть Poe по-настоящему
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx

from app.config import settings
from app.core.orchestrator import Orchestrator
from app.db.database import Database
from app.transports.base import SendResult
from app.transports.wazzup import WazzupTransport

# Консоль Windows по умолчанию cp1251 — принудительно UTF-8
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

CHANNEL_ID = "11111111-2222-3333-4444-555555555555"
FOREIGN_CHANNEL_ID = "99999999-9999-9999-9999-999999999999"


def payload(
    text: str,
    *,
    channel_id: str,
    chat_type: str,
    mid: str,
    echo: bool = False,
    author: str = "",
):
    return {
        "messages": [
            {
                "messageId": mid,
                "dateTime": "2026-08-17T10:00:00.000Z",
                "channelId": channel_id,
                "chatType": chat_type,
                "chatId": "555000111",
                "type": "text",
                "isEcho": echo,
                "contact": {"name": "Тест Клиент"},
                "text": text,
                "authorName": author,
                "status": "inbound" if not echo else "sent",
            }
        ]
    }


class FakeWazzup(WazzupTransport):
    """Тот же разбор, но отправка перехвачена."""

    def __init__(self, settings_obj, db):
        super().__init__(settings_obj, db)
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, chat_id, text, *, channel_id="", chat_type=""):
        channel_id, chat_type = self._resolve_route(chat_id, channel_id, chat_type)
        if not channel_id or not chat_type:
            print(f"    !! роутинг не найден для chat={chat_id}")
            return SendResult(ok=False, error="no route")
        self.sent.append((chat_id, text))
        print(f"    -> ОТПРАВКА в {chat_type}/{channel_id[:8]}…: {text[:160]}")
        return SendResult(ok=True, external_id=f"fake-{len(self.sent)}")


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    return ok


async def main(argv: list[str]) -> int:
    with_poe = "--with-poe" in argv
    failures = 0

    print("=== 1. Конфигурация ===")
    check("WAZZUP_API_KEY", bool(settings.wazzup_api_key),
          "" if settings.wazzup_api_key else "пуст — реальная отправка не заработает")
    check("POE_API_KEY", bool(settings.poe_api_key),
          "" if settings.poe_api_key else "пуст — диалог уйдёт в fallback")
    print(f"  TEST_MODE={settings.test_mode} "
          f"channels={sorted(settings.test_channel_id_set) or '—'} "
          f"types={sorted(settings.test_chat_type_set) or '—'}")

    # Настройки правим ДО сборки объектов: PoeClient забирает ключ в __init__
    settings.test_mode = True
    settings.test_channel_ids = CHANNEL_ID
    settings.test_chat_types = ""
    settings.fast_mode = True  # без пауз, короткий батч
    if not with_poe:
        settings.poe_api_key = ""  # без сети: диалог отдаст fallback

    tmp = Path(tempfile.mkdtemp(prefix="osnova_selftest_"))
    db = Database(tmp / "test.db")
    wazzup = FakeWazzup(settings, db)
    orch = Orchestrator(settings, db, wazzup)
    wait = 25.0 if with_poe else 3.0

    print("\n=== 2. Разбор вебхука ===")
    msgs = wazzup.parse_webhook(
        payload("Здравствуйте", channel_id=CHANNEL_ID, chat_type="telegram", mid="m1")
    )
    failures += not check("одно сообщение разобрано", len(msgs) == 1, f"получено {len(msgs)}")
    if msgs:
        m = msgs[0]
        failures += not check("chat_id", m.chat_id == "555000111", m.chat_id)
        failures += not check("channel_id", m.channel_id == CHANNEL_ID, m.channel_id)
        failures += not check("chat_type", m.channel == "telegram", m.channel)

    outgoing = wazzup.parse_webhook(
        {"messages": [{"messageId": "x", "chatId": "555000111", "chatType": "telegram",
                       "channelId": CHANNEL_ID, "status": "delivered", "isEcho": False,
                       "type": "text", "text": "наш ответ"}]}
    )
    failures += not check("свои исходящие отфильтрованы", outgoing == [])

    print("\n=== 3. Фильтр тестового режима ===")
    failures += not check("разрешённый канал пропущен",
                          wazzup.channel_allowed(CHANNEL_ID, "telegram"))
    failures += not check("чужой канал отсечён",
                          not wazzup.channel_allowed(FOREIGN_CHANNEL_ID, "whatsapp"))

    print("\n=== 4. Чужой канал не доходит до диалога ===")
    await orch.handle_webhook_payload(
        payload("Привет с ватсапа", channel_id=FOREIGN_CHANNEL_ID,
                chat_type="whatsapp", mid="m2")
    )
    failures += not check("чат из чужого канала не заведён",
                          db.get_chat("555000111") is None)

    print("\n=== 5. Gatekeeper и роутинг ===")
    await orch.handle_webhook_payload(
        payload("Здравствуйте, нужен шкаф-купе", channel_id=CHANNEL_ID,
                chat_type="telegram", mid="m3")
    )
    await asyncio.sleep(wait)

    chat = db.get_chat("555000111")
    failures += not check("чат заведён", chat is not None)
    if chat:
        failures += not check("статус new", chat["status"] == "new", str(chat["status"]))
        failures += not check("channel_id сохранён", chat["channel_id"] == CHANNEL_ID)
        failures += not check("chat_type сохранён", chat["chat_type"] == "telegram")
    failures += not check("ответ отправлен", len(wazzup.sent) == 1,
                          f"отправлено {len(wazzup.sent)}")

    print("\n=== 6. Дедуп повторного вебхука ===")
    before = len(wazzup.sent)
    await orch.handle_webhook_payload(
        payload("Здравствуйте, нужен шкаф-купе", channel_id=CHANNEL_ID,
                chat_type="telegram", mid="m3")
    )
    await asyncio.sleep(wait)
    failures += not check("дубль не обработан", len(wazzup.sent) == before)

    print("\n=== 7. Менеджер ответил руками -> manual ===")
    # Автоответ площадки приходит тем же эхом, но без автора — он не должен
    # уводить чат в manual, иначе бот замолкает после первого же приветствия
    await orch.handle_webhook_payload(
        payload("Здравствуйте! Спасибо, что написали. Мы скоро ответим.",
                channel_id=CHANNEL_ID, chat_type="telegram", mid="m3a", echo=True)
    )
    chat = db.get_chat("555000111")
    failures += not check(
        "автоответ площадки не перехватывает чат",
        chat and chat["status"] == "new",
        str(chat["status"]) if chat else "нет чата",
    )

    await orch.handle_webhook_payload(
        payload("Я сам отвечу этому клиенту", channel_id=CHANNEL_ID,
                chat_type="telegram", mid="m4", echo=True, author="Пётр")
    )
    chat = db.get_chat("555000111")
    failures += not check("статус manual", chat and chat["status"] == "manual",
                          str(chat["status"]) if chat else "нет чата")

    before = len(wazzup.sent)
    await orch.handle_webhook_payload(
        payload("А сколько будет стоить?", channel_id=CHANNEL_ID,
                chat_type="telegram", mid="m5")
    )
    await asyncio.sleep(wait)
    failures += not check("в manual бот молчит", len(wazzup.sent) == before)

    print("\n=== 8. #старт возвращает бота ===")
    await orch.handle_webhook_payload(
        payload("#старт", channel_id=CHANNEL_ID, chat_type="telegram",
                mid="m6", echo=True)
    )
    chat = db.get_chat("555000111")
    failures += not check("статус new", chat and chat["status"] == "new",
                          str(chat["status"]) if chat else "нет чата")

    print("\n=== 9. Режим тишины (WAZZUP_SEND_ENABLED=0) ===")
    # Настоящий транспорт, не подменённый: проверяем, что наружу нет запроса
    real = WazzupTransport(settings, db)
    calls: list[str] = []

    class _Tripwire:
        is_closed = False

        async def post(self, url, **kw):
            calls.append(url)
            # сетевая ошибка: транспорт ловит её штатно, тест не падает
            raise httpx.ConnectError("tripwire: запрос наружу перехвачен")

    real._client = _Tripwire()  # type: ignore[assignment]
    settings.wazzup_api_key = settings.wazzup_api_key or "test-key"
    settings.wazzup_send_enabled = False

    res = await real.send_text("555000111", "Этот текст не должен уйти")
    failures += not check("send_text вернул ok", res.ok)
    failures += not check("наружу ни одного запроса", not calls)
    failures += not check("нет фиктивного external_id", res.external_id == "")

    settings.wazzup_send_enabled = True
    res2 = await real.send_text("555000111", "А это уже пошло бы")
    failures += not check("с включённой отправкой запрос идёт", bool(calls),
                          "перехвачен" if calls else "запроса не было")
    failures += not check("и он падает на перехватчике", not res2.ok)

    # Предохранитель: даже с включённой отправкой наружу уходит только
    # разрешённый канал. Дожимы и релей цены шлют по роутингу из БД, а не в
    # ответ на входящее, поэтому фильтра на приёме здесь недостаточно.
    db.remember_route("555000999", channel_id=FOREIGN_CHANNEL_ID, chat_type="whatsapp")
    before_calls = len(calls)
    res3 = await real.send_text("555000999", "В чужой канал уйти не должно")
    failures += not check("чужой канал заблокирован на отправке", not res3.ok, res3.error)
    failures += not check("наружу запроса не было", len(calls) == before_calls)
    settings.wazzup_send_enabled = False

    print("\n=== 10. Дожимы: причина срыва решает текст ===")
    from datetime import datetime, timedelta, timezone

    from app.core import stall

    failures += not check(
        "возражение -> objection",
        stall.reason_for("objection") == stall.OBJECTION,
    )
    failures += not check(
        "готов заказать -> ready_stalled",
        stall.reason_for("ready_to_order") == stall.READY_STALLED,
    )
    failures += not check(
        "ремонт не готов -> deferred",
        stall.reason_for("deferred_demand") == stall.DEFERRED,
    )
    failures += not check(
        "неизвестный интент -> ghosted",
        stall.reason_for(None) == stall.GHOSTED,
    )
    failures += not check(
        "незакрытый расчёт перебивает интент",
        stall.reason_for("objection", has_pending_price=True) == stall.WAITING_US,
    )
    failures += not check(
        "отказавшемуся не пишем никогда",
        stall.next_followup(stall.REFUSED, stage=0, silent_hours=1000) is None,
    )
    failures += not check(
        "ждущему нас не пишем никогда",
        stall.next_followup(stall.WAITING_US, stage=0, silent_hours=1000) is None,
    )
    failures += not check(
        "рано — молчим",
        stall.next_followup(stall.GHOSTED, stage=0, silent_hours=1, base_hours=4) is None,
    )
    failures += not check(
        "ступени не бесконечны",
        stall.next_followup(stall.GHOSTED, stage=99, silent_hours=1000) is None,
    )

    # Тихие часы иначе зарубят дожим в зависимости от времени прогона
    settings.push_hour_start, settings.push_hour_end = 0, 24

    def _silent_for(hours: float, intent: str) -> None:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        db.upsert_chat(
            "555000111",
            status="new",
            last_bot_msg_at=stamp,
            last_user_msg_at=(
                datetime.now(timezone.utc) - timedelta(hours=hours + 1)
            ).isoformat(),
            last_intent=intent,
            followup_stage=0,
        )

    # Попросил время подумать — через 5 часов дёргать нельзя
    _silent_for(5, "objection")
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "взявшего паузу через 5 часов не трогаем", len(wazzup.sent) == before
    )

    # Через сутки — можно, и текстом про сомнения, а не дежурным
    _silent_for(25, "objection")
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    sent_objection = wazzup.sent[before:]
    failures += not check(
        "через сутки возражение дожато", len(sent_objection) == 1,
        f"отправлено {len(sent_objection)}"
    )
    if sent_objection:
        failures += not check(
            "текст помнит про паузу, а не дежурный",
            "не тороплю" in sent_objection[0][1],
            sent_objection[0][1][:60],
        )
    chat = db.get_chat("555000111")
    failures += not check(
        "ступень выросла",
        bool(chat) and int(chat.get("followup_stage") or 0) == 1,
        str(chat.get("followup_stage") if chat else "нет чата"),
    )

    # Просто откололся: первая попытка через 4 часа, вторая через двое суток
    _silent_for(5, "product_question")
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "пропавшего молча дожимаем через 4 часа", len(wazzup.sent) == before + 1
    )
    db.upsert_chat("555000111", followup_stage=1)
    _silent_for(30, "product_question")
    db.upsert_chat("555000111", followup_stage=1)
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "через 30 часов вторая попытка ещё рано", len(wazzup.sent) == before
    )
    _silent_for(50, "product_question")
    db.upsert_chat("555000111", followup_stage=1)
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "через двое суток вторая и последняя попытка",
        len(wazzup.sent) == before + 1,
    )
    _silent_for(200, "product_question")
    db.upsert_chat("555000111", followup_stage=2)
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "третьего дожима не бывает", len(wazzup.sent) == before
    )

    # Клиент попросил вернуться через конкретный срок
    from app.core.markers import extract as extract_markers

    _clean, mk = extract_markers(
        "Хорошо, напишу позже.\n[[ОТЛОЖИТЬ: 3 | клиент просил через 3 дня]]"
    )
    failures += not check("маркер отсрочки разобран", mk.snooze_days == 3, str(mk.snooze_days))
    failures += not check(
        "причина отсрочки сохранена",
        (mk.snooze_reason or "").startswith("клиент просил"),
        str(mk.snooze_reason),
    )

    _silent_for(100, "product_question")
    db.snooze_chat("555000111", 3, "клиент просил через 3 дня")
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "до оговорённого срока молчим, даже если пропал 100 часов назад",
        len(wazzup.sent) == before,
    )
    chat = db.get_chat("555000111")
    failures += not check(
        "ступени сброшены договорённостью",
        bool(chat) and int(chat.get("followup_stage") or 0) == 0,
        str(chat.get("followup_stage") if chat else "нет"),
    )

    # Срок наступил
    db.upsert_chat(
        "555000111",
        followup_due_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    sent_snooze = wazzup.sent[before:]
    failures += not check("в срок вернулись", len(sent_snooze) == 1)
    if sent_snooze:
        failures += not check(
            "текст ссылается на договорённость",
            "как и договаривались" in sent_snooze[0][1],
            sent_snooze[0][1][:70],
        )
    chat = db.get_chat("555000111")
    failures += not check(
        "отсрочка снята после возврата",
        bool(chat) and not chat.get("followup_due_at"),
        str(chat.get("followup_due_at") if chat else "нет"),
    )

    _silent_for(5, "refusal")
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check("после отказа молчим", len(wazzup.sent) == before)

    _silent_for(5, "objection")
    db.open_price_request(
        request_id="testreq01", chat_id="555000111", summary="тест", ask="тест"
    )
    before = len(wazzup.sent)
    await orch.followup_job.tick()
    failures += not check(
        "пока расчёт не отправлен — клиента не дёргаем", len(wazzup.sent) == before
    )
    db.close_price_request("testreq01", delivered=True)

    print("\n=== 11. Голосовые и фото ===")
    from app.services import media as media_mod
    from app.services import transcription as tr_mod
    from app.transports.base import IncomingMessage

    failures += not check(
        "галлюцинация Whisper отбракована",
        tr_mod.unclear("Субтитры сделал DimaTorzok"),
    )
    failures += not check(
        "подпись фонового звука отбракована", tr_mod.unclear("(СПОКОЙНАЯ МУЗЫКА)")
    )
    failures += not check(
        "зацикленный шум отбракован", tr_mod.unclear("Спасибо. Спасибо. Спасибо.")
    )
    failures += not check(
        "живая речь проходит",
        not tr_mod.unclear("Здравствуйте, нужен шкаф купе в спальню два метра"),
    )
    failures += not check(
        "удвоенная расшифровка схлопнута",
        tr_mod._dedupe("нужен шкаф купе нужен шкаф купе") == "нужен шкаф купе",
    )

    voice_msg = IncomingMessage(
        chat_id="555000111",
        message_id="voice-1",
        kind="voice",
        media_url="https://example.invalid/voice.ogg",
    )
    orig_download, orig_transcribe = media_mod.download, tr_mod.transcribe_bytes

    async def _fake_download(url, *, max_bytes=None):
        return b"fake-audio-bytes"

    async def _fake_transcribe(data, suffix=".ogg"):
        return "Здравствуйте, нужен шкаф купе в спальню два метра"

    media_mod.download = _fake_download
    tr_mod.transcribe_bytes = _fake_transcribe
    text, hints = await orch.dialog._read_voice([voice_msg])
    failures += not check("голосовое расшифровано", "шкаф купе" in text, text[:50])
    failures += not check(
        "модели запрещено переспрашивать",
        any("правильно ли я понял" in h for h in hints),
    )

    async def _fake_garbage(data, suffix=".ogg"):
        return "Субтитры сделал DimaTorzok"

    tr_mod.transcribe_bytes = _fake_garbage
    text2, hints2 = await orch.dialog._read_voice([voice_msg])
    failures += not check("мусор не попал в диалог", text2 == "", text2[:40])
    failures += not check(
        "вместо мусора просим повторить",
        any("не расслышал" in h for h in hints2),
    )

    photo_msg = IncomingMessage(
        chat_id="555000111",
        message_id="photo-1",
        kind="image",
        media_url="https://example.invalid/photo.jpg",
    )
    settings.vision_enabled = False
    ph_hints, ph_matches = await orch.dialog._read_photos([photo_msg])
    failures += not check("без vision честно говорим о недоступности", bool(ph_hints))
    failures += not check("совпадений нет", ph_matches == [])

    settings.vision_enabled = True

    async def _fake_search(data, *, filename="photo.jpg", top_k=5,
                           colors=None, types=None):
        return {
            "found": True,
            "matches": [
                {
                    "article": "AV-10004515",
                    "name": "Обувница №2",
                    "price": "4 950 ₽",
                    "similarity": 0.81,
                    "photo_path": "catalog/AV-10004515/01.jpg",
                }
            ],
        }

    orch.dialog.vision.search_bytes = _fake_search
    ph_hints2, ph_matches2 = await orch.dialog._read_photos([photo_msg])
    failures += not check(
        "найденная модель попала в подсказку",
        any("Обувница №2" in h for h in ph_hints2),
    )
    failures += not check("совпадение вернулось", len(ph_matches2) == 1)
    # Домен берём фиксированный: тест проверяет сборку ссылки, а не .env
    saved_public = settings.public_webhook_url
    settings.public_webhook_url = "https://example.test/"
    failures += not check(
        "ссылка на фото собирается публичной",
        settings.public_media_url("catalog/AV-10004515/01.jpg")
        == "https://example.test/catalog/AV-10004515/01.jpg",
        settings.public_media_url("catalog/AV-10004515/01.jpg"),
    )
    settings.public_webhook_url = ""
    failures += not check(
        "без домена ссылку не выдумываем",
        settings.public_media_url("catalog/AV-10004515/01.jpg") == "",
    )
    settings.public_webhook_url = saved_public

    media_mod.download, tr_mod.transcribe_bytes = orig_download, orig_transcribe
    settings.vision_enabled = False

    print("\n=== 12. MAX: кнопки, ожидание ответа, параллельные заявки ===")
    from app.jobs.max_inbox import MaxInbox
    from app.jobs.price_relay import PriceRelay
    from app.notify.max import ANSWER_PREFIX, SKIP_PREFIX, MaxNotifier

    MAX_CHAT = "12345"
    settings.max_enabled = True
    settings.max_bot_token = "test-token"
    settings.max_group_id = MAX_CHAT

    class FakeMax(MaxNotifier):
        """Тот же класс, но без сети: карточки и ответы копятся в списках."""

        def __init__(self, settings_obj):
            super().__init__(settings_obj)
            self.cards: list[str] = []
            self.notes: list[str] = []
            self._n = 0

        async def send(self, text, *, buttons=None):
            self._n += 1
            mid = f"max-mid-{self._n}"
            self.cards.append(text)
            return mid

        async def answer_callback(self, callback_id, notification=""):
            self.notes.append(notification)
            return True

    fake_max = FakeMax(settings)
    relay = PriceRelay(db, wazzup, fake_max)
    inbox = MaxInbox(settings, db, fake_max, relay)

    def cb(payload: str, *, user: str = "7001", cid: str = "cb-1") -> dict:
        return {
            "update_type": "message_callback",
            "callback": {
                "callback_id": cid,
                "payload": payload,
                "user": {"user_id": int(user)},
            },
            "message": {"recipient": {"chat_id": int(MAX_CHAT)}},
        }

    def owner_msg(text: str, *, user: str = "7001", reply_mid: str = "") -> dict:
        msg = {
            "sender": {"user_id": int(user), "is_bot": False},
            "recipient": {"chat_id": int(MAX_CHAT)},
            "body": {"mid": "in-1", "text": text},
        }
        if reply_mid:
            msg["link"] = {"type": "reply", "message": {"mid": reply_mid}}
        return {"update_type": "message_created", "message": msg}

    rid_a = await relay.on_client_wants_price(
        chat_id="555000111", summary="кухня 3 метра", ask="посчитать кухню"
    )
    failures += not check("заявка А заведена", bool(rid_a), str(rid_a))
    failures += not check("карточка ушла в MAX", len(fake_max.cards) == 1)

    cards_before = len(fake_max.cards)
    await inbox.handle_update(cb(f"{ANSWER_PREFIX}{rid_a}"))
    failures += not check(
        "после «Ответить» ждём текст",
        db.get_awaiting(MAX_CHAT, "7001") == rid_a,
    )
    # Всплывашку MAX не показывает, поэтому нажатие обязано оставить
    # видимое сообщение — иначе сотрудник решит, что кнопка не сработала
    failures += not check(
        "нажатие видно в чате",
        len(fake_max.cards) == cards_before + 1
        and "Жду ваш ответ" in fake_max.cards[-1],
        fake_max.cards[-1][:50] if fake_max.cards else "нет сообщений",
    )

    # Пока сотрудник печатает ответ по А, второй клиент тоже просит расчёт.
    # Заявка Б не должна сбить режим ожидания, открытый на А.
    db.remember_route("555000222", channel_id=CHANNEL_ID, chat_type="telegram")
    rid_b = await relay.on_client_wants_price(
        chat_id="555000222", summary="шкаф купе", ask="посчитать шкаф"
    )
    failures += not check("заявка Б заведена", bool(rid_b) and rid_b != rid_a)
    failures += not check(
        "новая заявка не сбила ожидание",
        db.get_awaiting(MAX_CHAT, "7001") == rid_a,
        f"ожидание на {db.get_awaiting(MAX_CHAT, '7001')}, а было {rid_a}",
    )

    before = len(wazzup.sent)
    await inbox.handle_update(owner_msg("Кухня 3 метра — 145 000 рублей"))
    sent_now = wazzup.sent[before:]
    failures += not check("ответ ушёл клиенту", len(sent_now) == 1)
    if sent_now:
        failures += not check(
            "текст владельца передан как есть",
            "145 000" in sent_now[0][1],
            sent_now[0][1][:60],
        )
    row_a = db.get_price_by_request_id(rid_a)
    failures += not check(
        "заявка А закрыта", row_a and row_a["status"] == "delivered",
        str(row_a.get("status") if row_a else "нет"),
    )
    failures += not check(
        "ожидание снято", db.get_awaiting(MAX_CHAT, "7001") is None
    )
    failures += not check(
        "подтверждение называет чат клиента",
        "555000111" in fake_max.cards[-1],
        fake_max.cards[-1][:60],
    )

    # Второй сотрудник отвечает реплаем на карточку — без всякой кнопки
    row_b = db.get_price_by_request_id(rid_b)
    before = len(wazzup.sent)
    await inbox.handle_update(
        owner_msg("Шкаф — 92 000", user="7002", reply_mid=row_b["max_message_id"])
    )
    failures += not check("реплай на карточку сработал", len(wazzup.sent) == before + 1)

    # Посторонняя болтовня в чате клиенту не уходит
    before = len(wazzup.sent)
    await inbox.handle_update(owner_msg("обед в 14:00", user="7003"))
    failures += not check("посторонняя реплика не ушла", len(wazzup.sent) == before)

    # «Пропустить» закрывает заявку и возвращает чат в обычные дожимы
    rid_c = await relay.on_client_wants_price(
        chat_id="555000111", summary="прихожая", ask="посчитать прихожую"
    )
    await inbox.handle_update(cb(f"{SKIP_PREFIX}{rid_c}", cid="cb-2"))
    row_c = db.get_price_by_request_id(rid_c)
    failures += not check(
        "«Пропустить» закрыло заявку",
        row_c and row_c["status"] == "skipped",
        str(row_c.get("status") if row_c else "нет"),
    )
    failures += not check(
        "после пропуска чат снова дожимается",
        db.get_pending_price("555000111") is None,
    )

    # Протухшее ожидание не перехватывает чужие сообщения
    rid_d = await relay.on_client_wants_price(
        chat_id="555000111", summary="кровать", ask="посчитать кровать"
    )
    await inbox.handle_update(cb(f"{ANSWER_PREFIX}{rid_d}", cid="cb-3"))
    failures += not check(
        "протухшее ожидание не отдаётся",
        db.get_awaiting(MAX_CHAT, "7001", ttl_minutes=-1) is None,
    )

    settings.max_enabled = False
    settings.max_bot_token = ""

    print("\n=== 13. Подбор по каталогу ===")
    from app.catalog import vocab as cat_vocab
    from app.catalog.search import shared as catalog_shared

    cat = catalog_shared()
    failures += not check("каталог загружен", cat.loaded, f"{cat.size} позиций")

    if cat.loaded:
        corner = cat.search("какие угловые диваны у вас бывают?", limit=3)
        failures += not check("угловые диваны найдены", len(corner) == 3)
        failures += not check(
            "все выданные — угловые диваны",
            all(i["type"] == "диван" and "угловой" in i["features"] for i in corner),
            ", ".join(f"{i['type']}/{i['features']}" for i in corner)[:80],
        )

        # Ровно тот случай, о котором просил клиент: белая столешница не
        # должна приводить к белому дивану
        tops = cat.search("нужна белая столешница", limit=3)
        failures += not check(
            "белая столешница ведёт к столам, а не к диванам",
            bool(tops) and all(i["type"] == "стол" for i in tops),
            ", ".join(i["type"] for i in tops) or "пусто",
        )

        failures += not check(
            "«бельевой короб» не считается белым цветом",
            "белый" not in cat_vocab.detect_colors("диван с бельевым коробом"),
        )
        failures += not check(
            "морфология: «угловые» = «угловой»",
            "угловой" in cat_vocab.detect_features("какие угловые бывают"),
        )
        failures += not check(
            "неизвестный тип не подменяется другим",
            cat.search("нужен аквариум", furniture="аквариум") == [],
        )

        picked = orch.dialog._pick_from_catalog(
            "покажите угловые диваны", "catalog_request", {}, {"furniture": "диван"}
        )
        failures += not check("диалог подбирает под catalog_request", len(picked) == 3)
        failures += not check(
            "на приветствие каталог не вываливается",
            orch.dialog._pick_from_catalog("здравствуйте", "greeting", {}, {}) == [],
        )
        failures += not check(
            "у подобранного есть ссылка на фото",
            bool(picked and picked[0].get("photos")),
        )

        # Название модели клиенту ничего не говорит: подпись под фото обязана
        # нести цену и признаки, иначе «Дерби» остаётся пустым звуком
        caption = Orchestrator._photo_caption(picked[0]) if picked else ""
        failures += not check(
            "в подписи под фото есть цена",
            "руб." in caption,
            caption[:70],
        )
        failures += not check(
            "в подписи есть признаки, а не только имя",
            caption.count("—") >= 2,
            caption[:70],
        )

    await orch.shutdown()
    print("\n" + "=" * 52)
    if failures:
        print(f"ПРОВАЛЕНО ПРОВЕРОК: {failures}")
    else:
        print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
    print(f"временная БД: {tmp}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
