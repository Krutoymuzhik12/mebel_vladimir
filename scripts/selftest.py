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


def payload(text: str, *, channel_id: str, chat_type: str, mid: str, echo: bool = False):
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
    await orch.handle_webhook_payload(
        payload("Я сам отвечу этому клиенту", channel_id=CHANNEL_ID,
                chat_type="telegram", mid="m4", echo=True)
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
