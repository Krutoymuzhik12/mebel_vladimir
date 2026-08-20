"""Проверка фич, добавленных после базового каркаса.

Ничего наружу не отправляет: Wazzup/Poe/MAX не дёргаются.

    python -m scripts.debug_new_features
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from app.config import ROOT, Settings
from app.core.gatekeeper import Gatekeeper
from app.core.orchestrator import Orchestrator
from app.db.database import Database
from app.jobs.avito_show_phone import AvitoShowPhoneJob
from app.jobs.document_relay import DocumentRelay
from app.jobs.price_relay import PriceRelay
from app.services import amocrm, attachments, backup
from app.services.process_lock import ExclusiveLock
from app.transports.base import SendResult
from app.transports.wazzup import WazzupTransport

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILURES.append(name)


def msg(mid: str, mtype: str, **extra):
    base = {
        "messageId": mid,
        "chatId": "chat-1",
        "channelId": "ch-1",
        "chatType": "telegram",
        "type": mtype,
        "status": "inbound",
        "text": "",
    }
    base.update(extra)
    return base


def test_parse_kinds(settings: Settings) -> None:
    w = WazzupTransport(settings)
    payload = {
        "messages": [
            msg("1", "text", text="привет"),
            msg("2", "image", contentUri="http://x/a.jpg"),
            msg("3", "voice", contentUri="http://x/a.ogg"),
            msg("4", "video", contentUri="http://x/a.mp4"),
            msg("5", "document", contentUri="http://x/plan.pdf"),
            msg("6", "sticker"),
            msg("7", "geo"),
            msg("8", "system", chatType="avito", text="Клиент показал номер"),
            msg("9", "weird_new_type", contentUri="http://x/f.bin"),
            msg("10", "weird_no_file"),
        ]
    }
    got = [m.kind for m in w.parse_webhook(payload)]
    want = [
        "text",
        "image",
        "voice",
        "video",
        "document",
        "sticker",
        "geo",
        "system",
        "document",
        "unsupported",
    ]
    check("parse_webhook: типы вложений", got == want, f"{got}")

    systems = [m for m in w.parse_webhook(payload) if m.is_system]
    check(
        "parse_webhook: is_system только у системных",
        len(systems) == 1 and systems[0].message_id == "8",
        f"{[m.message_id for m in systems]}",
    )

    avito = next(m for m in w.parse_webhook(payload) if m.message_id == "8")
    check("Авито show-phone распознан", w.looks_like_avito_show_phone(avito))

    # исходящее наше — не обрабатываем
    out = w.parse_webhook({"messages": [msg("11", "text", status="sent", text="ответ")]})
    check("статусные исходящие отфильтрованы", out == [])


def test_attachment_hints() -> None:
    check(
        "стикер без подсказки (не реагируем)",
        attachments.hint_for("sticker") == "",
    )
    check("видео без подсказки (фича убрана)", attachments.hint_for("video") == "")
    # PDF/DOCX больше не разбираем: документ целиком уходит владельцу через
    # MAX (DocumentRelay), модель его не видит — подсказки для неё нет.
    check(
        "документ: подсказки модели больше нет (уходит в MAX)",
        attachments.hint_for("document") == "",
    )
    check("geo: подсказка есть", bool(attachments.hint_for("geo")))
    check("unsupported: подсказка есть", bool(attachments.hint_for("unsupported")))


class SilentWazzup(WazzupTransport):
    def __init__(self, settings_obj, db):
        super().__init__(settings_obj, db)
        self.sent: list[str] = []

    async def send_text(self, chat_id, text, *, channel_id="", chat_type=""):
        self.sent.append(text)
        return SendResult(ok=True, external_id="fake")

    async def send_media(self, chat_id, file_path, caption="", *, channel_id="", chat_type=""):
        self.sent.append(caption)
        return SendResult(ok=True, external_id="fake")


async def test_ingest_ignores(settings: Settings, db_path: Path) -> None:
    db = Database(db_path)
    wazzup = SilentWazzup(settings, db)
    orch = Orchestrator(settings, db, wazzup)

    payload = {
        "messages": [
            msg("s1", "sticker"),
            msg("v1", "video", contentUri="http://x/a.mp4"),
        ]
    }
    for m in wazzup.parse_webhook(payload):
        await orch.ingest(m)

    history = db.recent_messages("chat-1", 10)
    check(
        "стикер/видео не попали в историю диалога",
        history == [],
        f"{[h.get('kind') for h in history]}",
    )
    check("стикер/видео не вызвали отправку", wazzup.sent == [])
    await orch.wazzup.aclose()
    await orch.max_notifier.aclose()


async def test_document_bypasses_dialog(settings: Settings, db_path: Path) -> None:
    """Документ (PDF/Word/др.) уходит в MAX напрямую из orchestrator.ingest(),
    минуя dialog.handle() и Poe. В этом тесте MAX выключен и POE_API_KEY пуст —
    если бы документ всё же дошёл до модели, клиент получил бы FALLBACK-текст
    диалога. Он не должен его получить ни при каком раскладе."""
    from app.core.dialog import FALLBACK

    db = Database(db_path)
    wazzup = SilentWazzup(settings, db)
    orch = Orchestrator(settings, db, wazzup)

    payload = {
        "messages": [
            msg("d1", "document", contentUri="http://x/plan.pdf", text="вот план")
        ]
    }
    for m in wazzup.parse_webhook(payload):
        await orch.ingest(m)

    history = db.recent_messages("chat-1", 10)
    kinds = [h.get("kind") for h in history]
    check("документ записан в историю", "document" in kinds, f"{kinds}")

    reply = wazzup.sent[-1] if wazzup.sent else ""
    check("клиенту что-то ответили", bool(reply))
    check(
        "это не ответ модели (FALLBACK), а текст DocumentRelay",
        bool(reply) and reply != FALLBACK,
        reply,
    )
    check(
        "текст объясняет судьбу файла (MAX выключен → «не могу передать»)",
        "специалист" in reply.lower(),
        reply,
    )
    await orch.wazzup.aclose()
    await orch.max_notifier.aclose()


async def test_max_notify_scope(db_path: Path) -> None:
    """Что MAX вправе прислать владельцу.

    Расчёты, файлы и Авито «показал номер» — да: по каждому владелец что-то
    делает. Алерты мониторинга остаются в логе, чтобы не размывать поток.
    """
    from app.notify.max import MaxNotifier
    from app.services.health_watch import HealthWatch

    db = Database(db_path)
    local = Settings(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False,
        max_enabled=True, max_bot_token="t", max_group_id="1",
        poe_api_key="", vision_enabled=False,
    )

    class CountingMax(MaxNotifier):
        def __init__(self, s):
            super().__init__(s)
            self.sent: list[str] = []

        async def send(self, text, *, buttons=None):
            self.sent.append(text)
            return "mid-1"

    wazzup = SilentWazzup(local, db)

    # Авито: событие уходит владельцу, клиенту при этом не пишем
    notifier = CountingMax(local)
    job = AvitoShowPhoneJob(db, wazzup, notifier, local)
    check("Авито-уведомления включены по умолчанию", job.enabled)
    avito = wazzup.parse_webhook(
        {"messages": [msg("a1", "system", chatType="avito", text="Клиент показал номер")]}
    )[0]
    await job.on_event(avito)
    check("Авито show-phone ушёл в MAX", len(notifier.sent) == 1, f"{notifier.sent}")
    check(
        "в тексте видно, что номер смотрели без звонка",
        notifier.sent and "не позвонил" in notifier.sent[0],
        notifier.sent[0][:60] if notifier.sent else "—",
    )
    check("клиенту при этом не написали", wazzup.sent == [], f"{wazzup.sent}")
    check(
        "доставлено → очередь ретраев пуста",
        db.candidates_show_phone() == [],
    )
    check("повторно не дублируется", await job.tick() == 0)

    # Мониторинг: проблема есть, но владельцу о ней не пишем
    watch_max = CountingMax(local)
    watch = HealthWatch(local, watch_max)
    status = await watch.check()
    check("монитор проблему видит", not status["ok"], f"{status['issues']}")
    check("но в MAX её не шлёт", watch_max.sent == [], f"{watch_max.sent}")

    # А расчёт и документ — уходят
    price_max = CountingMax(local)
    rid = await PriceRelay(db, wazzup, price_max).on_client_wants_price(
        chat_id="chat-1", summary="кухня", ask="посчитать"
    )
    check("расчёт в MAX уходит", bool(rid) and len(price_max.sent) == 1)

    doc_max = CountingMax(local)
    doc_rid = await DocumentRelay(db, wazzup, doc_max).on_client_sent_document(
        chat_id="chat-1", doc_url="http://x/plan.pdf"
    )
    check("файл в MAX уходит", bool(doc_rid) and len(doc_max.sent) == 1)

    for n in (notifier, watch_max, price_max, doc_max):
        await n.aclose()
    await wazzup.aclose()


class FakeMax:
    """Ловим пуши в MAX вместо реальной отправки."""

    def __init__(self, settings_obj: Settings, *, fail: bool = False) -> None:
        self.settings = settings_obj
        self.fail = fail
        self.avito: list[tuple[str, str]] = []

    async def avito_show_phone(self, *, chat_id: str, details: str = "") -> bool:
        self.avito.append((chat_id, details))
        return not self.fail

    async def send(self, *a, **kw):
        return ""

    async def aclose(self) -> None:
        return None


def avito_event(mid: str = "a1") -> dict:
    return msg(
        mid,
        "system",
        chatType="avito",
        text="Клиент показал номер телефона",
    )


async def test_avito_show_phone(settings: Settings, tmp: Path) -> None:
    db = Database(tmp / "avito.db")
    wazzup = SilentWazzup(settings, db)
    orch = Orchestrator(settings, db, wazzup)
    fake = FakeMax(settings)
    orch.max_notifier = fake
    orch.avito_job.max_notifier = fake

    event = wazzup.parse_webhook({"messages": [avito_event()]})[0]
    check("событие Авито распознано парсером", wazzup.looks_like_avito_show_phone(event))

    await orch.ingest(event)

    check("уведомление ушло в MAX", len(fake.avito) == 1, f"{fake.avito}")
    check(
        "в уведомлении верный чат",
        fake.avito and fake.avito[0][0] == "chat-1",
        f"{fake.avito[0][0] if fake.avito else '—'}",
    )
    check("клиенту НИЧЕГО не отправлено", wazzup.sent == [], f"{wazzup.sent}")
    check("в историю диалога не попало", db.recent_messages("chat-1", 10) == [])
    check("помечено как доставленное", db.candidates_show_phone() == [])

    await wazzup.aclose()


async def test_avito_retry(settings: Settings, tmp: Path) -> None:
    """MAX недоступен → notified не ставим, дошлёт tick()."""
    db = Database(tmp / "avito_retry.db")
    wazzup = SilentWazzup(settings, db)
    orch = Orchestrator(settings, db, wazzup)
    broken = FakeMax(settings, fail=True)
    orch.max_notifier = broken
    orch.avito_job.max_notifier = broken

    event = wazzup.parse_webhook({"messages": [avito_event("a2")]})[0]
    await orch.ingest(event)
    check(
        "MAX упал → событие осталось в очереди",
        len(db.candidates_show_phone()) == 1,
        f"{len(db.candidates_show_phone())}",
    )

    working = FakeMax(settings)
    orch.avito_job.max_notifier = working
    sent = await orch.avito_job.tick()
    check("tick дослал уведомление", sent == 1 and len(working.avito) == 1)
    check("очередь опустела", db.candidates_show_phone() == [])
    await wazzup.aclose()


async def test_avito_flag_off(tmp: Path) -> None:
    local = Settings(
        wazzup_api_key="k",
        wazzup_send_enabled=False,
        test_mode=False,
        poe_api_key="",
        max_enabled=False,
        vision_enabled=False,
        backup_enabled=False,
        health_watch_enabled=False,
        max_notify_avito=False,
    )
    db = Database(tmp / "avito_off.db")
    wazzup = SilentWazzup(local, db)
    orch = Orchestrator(local, db, wazzup)
    fake = FakeMax(local)
    orch.avito_job.max_notifier = fake

    event = wazzup.parse_webhook({"messages": [avito_event("a3")]})[0]
    await orch.ingest(event)
    check("MAX_NOTIFY_AVITO=0 → в MAX не пишем", fake.avito == [])
    check(
        "выключенный флаг не копит очередь",
        db.candidates_show_phone() == [],
        f"{len(db.candidates_show_phone())}",
    )
    await wazzup.aclose()


def test_avito_defaults() -> None:
    check(
        "MAX_NOTIFY_AVITO включён по умолчанию",
        Settings().max_notify_avito is True,
    )


async def test_send_retries(settings: Settings, db_path: Path) -> None:
    """Ретраи: 500 повторяем, 400 — нет."""
    import httpx

    db = Database(db_path)
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, code: int) -> None:
            self.status_code = code
            self.text = "boom"
            self.content = b"{}"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, code: int) -> None:
            self.code = code
            self.is_closed = False

        async def post(self, *a, **kw):
            calls["n"] += 1
            return FakeResp(self.code)

        async def aclose(self):
            self.is_closed = True

    local = Settings(
        wazzup_api_key="k",
        wazzup_send_enabled=True,
        test_mode=False,
        wazzup_send_retries=3,
        wazzup_send_retry_backoff_sec=0.01,
    )
    w = WazzupTransport(local, db)
    w._client = FakeClient(500)  # noqa: SLF001
    res = await w.send_text("chat-1", "текст", channel_id="ch", chat_type="telegram")
    check("5xx: 3 попытки", calls["n"] == 3, f"попыток={calls['n']}")
    check("5xx: результат not ok", not res.ok)

    calls["n"] = 0
    w._client = FakeClient(400)  # noqa: SLF001
    res = await w.send_text("chat-1", "текст", channel_id="ch", chat_type="telegram")
    check("4xx: без ретраев", calls["n"] == 1, f"попыток={calls['n']}")

    calls["n"] = 0
    w._client = FakeClient(429)  # noqa: SLF001
    await w.send_text("chat-1", "текст", channel_id="ch", chat_type="telegram")
    check("429: ретраится", calls["n"] == 3, f"попыток={calls['n']}")

    _ = httpx  # импорт нужен только для наглядности типов


async def test_dry_run_guard(db_path: Path) -> None:
    db = Database(db_path)
    local = Settings(wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False)
    w = WazzupTransport(local, db)
    called = {"n": 0}

    class Boom:
        is_closed = False

        async def post(self, *a, **kw):
            called["n"] += 1
            raise AssertionError("в dry-run наружу уходить не должно")

    w._client = Boom()  # noqa: SLF001
    res = await w.send_text("chat-1", "привет", channel_id="ch", chat_type="telegram")
    check("dry-run: HTTP не вызван", called["n"] == 0)
    check("dry-run: ok=True без external_id", res.ok and not res.external_id)


async def test_amocrm_baseline(tmp: Path) -> None:
    baseline = tmp / "existing.txt"
    baseline.write_text(
        "# комментарий\n79001234567\n\n555000111  # старый чат\n79001234567\n",
        encoding="utf-8",
    )
    local = Settings(amocrm_token="", amocrm_baseline_file=str(baseline))
    ids = await amocrm.baseline_existing_ids(local)
    check(
        "amoCRM baseline: файл читается, дубли убраны",
        ids == ["79001234567", "555000111"],
        f"{ids}",
    )

    probe = amocrm.probe_config(local)
    check("amoCRM probe: stub=True без токена", probe["stub"] and not probe["configured"])

    stub = Settings(amocrm_token="REPLACE_WHEN_READY", amocrm_base_url="https://x.amocrm.ru")
    check(
        "amoCRM: REPLACE_WHEN_READY считается заглушкой",
        amocrm.probe_config(stub)["stub"],
    )
    ids2 = await amocrm.baseline_existing_ids(stub)
    check("amoCRM: с заглушкой API не дёргается", ids2 == [], f"{ids2}")


async def test_amocrm_extraction(tmp: Path) -> None:
    """Разбор ответа amoCRM без сети.

    Имена полей сверены с живым аккаунтом клиента: коннектор Wazzup кладёт
    точный chatId в отдельное поле под каждый канал. Телефон приводим к виду,
    в котором Wazzup отдаёт chatId для WhatsApp (79XXXXXXXXX).
    """
    for raw, want in (
        ("89069235000", "79069235000"),
        ("+7 906 923-50-00", "79069235000"),
        ("9069235000", "79069235000"),
        ("79069235000", "79069235000"),
        ("не телефон", ""),
        ("123", ""),
    ):
        check(f"телефон {raw!r} → {want!r}", amocrm.normalize_phone(raw) == want,
              amocrm.normalize_phone(raw))

    contact = {
        "id": 1,
        "custom_fields_values": [
            {"field_name": "TelegramId_WZ", "values": [{"value": "7936875555"}]},
            {"field_name": "Avito_WZ", "values": [{"value": "u2i-OVbsXpRMhK"}]},
            {"field_name": "Телефон", "values": [{"value": "89069235000"}]},
            {"field_name": "Должность", "values": [{"value": "директор"}]},
        ],
    }
    keys = amocrm._keys_from_contact(contact)
    check(
        "берём id чатов и телефон, не берём посторонние поля",
        set(keys) == {"7936875555", "u2i-OVbsXpRMhK", "79069235000"},
        f"{keys}",
    )

    # Исключения: тестовый чат не должен уйти в «старые» даже из CRM
    baseline = tmp / "excl.txt"
    baseline.write_text("7936875555\n79069235000\n", encoding="utf-8")
    local = Settings(
        amocrm_token="",
        amocrm_baseline_file=str(baseline),
        baseline_exclude_chat_ids="7936875555",
    )
    ids = await amocrm.baseline_existing_ids(local)
    check(
        "исключённый чат не попал в baseline",
        ids == ["79069235000"],
        f"{ids}",
    )


def test_gatekeeper_baseline(db_path: Path) -> None:
    db = Database(db_path)
    gate = Gatekeeper(db)
    n = gate.baseline_many(["old-1", "old-2"])
    check("baseline_many отметил чаты", n == 2, f"n={n}")
    check("old-1 → existing", gate.status("old-1") == "existing")
    check("бот молчит в existing", not gate.bot_may_reply("old-1"))
    gate.on_staff_message("old-1", "#старт")
    check(
        "#старт не оживляет existing",
        gate.status("old-1") == "existing",
        gate.status("old-1") or "",
    )


def test_backup(tmp: Path) -> None:
    db_file = tmp / "bot.db"
    db_file.write_bytes(b"sqlite-ish")
    out = tmp / "backups"
    made = [backup.backup_db(db_file, out, keep=2) for _ in range(3)]
    check("бэкап создаётся", all(m is not None for m in made))
    files = sorted(out.glob("bot_*.db"))
    check("ротация: не больше keep", len(files) <= 2, f"файлов={len(files)}")

    missing = backup.backup_db(tmp / "nope.db", out, keep=2)
    check("нет БД → None, без падения", missing is None)


def test_lock(tmp: Path) -> None:
    path = tmp / "max.lock"
    first = ExclusiveLock(path)
    check("лок берётся", first.acquire())
    second = ExclusiveLock(path)
    check("второй лок не берётся", not second.acquire())
    first.release()
    third = ExclusiveLock(path)
    check("после release лок снова свободен", third.acquire())
    third.release()


def test_voice_cap() -> None:
    from app.services import transcription

    async def run():
        big = b"x" * (Settings().voice_max_bytes + 10)
        return await transcription.transcribe_bytes(big, suffix=".ogg")

    out = asyncio.run(run())
    check(
        "длинное голосовое отсекается до расшифровки",
        out == "[голосовое слишком длинное]",
        repr(out),
    )
    check("длинное голосовое считается неразобранным", transcription.unclear(out))


async def test_health_watch() -> None:
    from app.notify.max import MaxNotifier
    from app.services.health_watch import HealthWatch

    local = Settings(poe_api_key="", max_enabled=False, db_path="data/bot.db")
    watch = HealthWatch(local, MaxNotifier(local))
    status = await watch.check()
    check(
        "health: без POE_API_KEY поднимает проблему",
        not status["ok"] and any("Poe" in i for i in status["issues"]),
        f"{status['issues']}",
    )


def main() -> None:
    settings = Settings(
        wazzup_api_key="test-key",
        wazzup_send_enabled=False,
        test_mode=False,
        poe_api_key="",
        max_enabled=False,
        vision_enabled=False,
        backup_enabled=False,
        health_watch_enabled=False,
        fast_mode=True,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_path = tmp / "test.db"

        print("\n--- Разбор вебхука ---")
        test_parse_kinds(settings)

        print("\n--- Вложения (PDF/стикеры/видео) ---")
        test_attachment_hints()

        print("\n--- Игнор стикеров и видео в пайплайне ---")
        asyncio.run(test_ingest_ignores(settings, db_path))

        print("\n--- Авито «показал номер» → MAX ---")
        test_avito_defaults()
        asyncio.run(test_avito_show_phone(settings, tmp))
        asyncio.run(test_avito_retry(settings, tmp))
        asyncio.run(test_avito_flag_off(tmp))

        print("\n--- Документ уходит в MAX, минуя модель ---")
        asyncio.run(test_document_bypasses_dialog(settings, tmp / "doc.db"))

        print("\n--- MAX: расчёты, файлы, Авито (без алертов мониторинга) ---")
        asyncio.run(test_max_notify_scope(tmp / "scope.db"))

        print("\n--- Ретраи отправки ---")
        asyncio.run(test_send_retries(settings, tmp / "retry.db"))

        print("\n--- Предохранитель dry-run ---")
        asyncio.run(test_dry_run_guard(tmp / "dry.db"))

        print("\n--- amoCRM baseline (заглушка) ---")
        asyncio.run(test_amocrm_baseline(tmp))

        print("\n--- amoCRM: разбор полей и исключения ---")
        asyncio.run(test_amocrm_extraction(tmp))

        print("\n--- Gatekeeper existing ---")
        test_gatekeeper_baseline(tmp / "gate.db")

        print("\n--- Бэкапы ---")
        test_backup(tmp)

        print("\n--- Лок MAX inbox ---")
        test_lock(tmp)

        print("\n--- Потолок на голосовые ---")
        test_voice_cap()

        print("\n--- Монитор здоровья ---")
        asyncio.run(test_health_watch())

    print("\n" + "=" * 50)
    if FAILURES:
        print(f"ПРОВАЛЕНО {len(FAILURES)}:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("Все проверки пройдены")
    _ = ROOT


if __name__ == "__main__":
    main()
