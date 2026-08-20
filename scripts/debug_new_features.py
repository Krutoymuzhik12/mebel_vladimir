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


async def test_first_contact_policy(tmp: Path) -> None:
    """Главное требование: бот берёт только тех, кто пришёл впервые.

    Отличить старого клиента от нового по данным Wazzup нельзя, поэтому при
    политике safe незнакомый чат считается старым и бот молчит. Ловушка,
    которая это однажды отменила: remember_route делает upsert_chat, а тот
    заводит строку со статусом new — если вызвать его до решения, чат станет
    «нашим» раньше, чем мы успели спросить.
    """
    common = dict(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False,
        poe_api_key="", max_enabled=False, vision_enabled=False,
        backup_enabled=False, health_watch_enabled=False, fast_mode=True,
    )

    # safe: незнакомый чат — молчим
    safe = Settings(**common, first_contact_policy="safe")
    db = Database(tmp / "fc_safe.db")
    w = SilentWazzup(safe, db)
    orch = Orchestrator(safe, db, w)
    for m in w.parse_webhook({"messages": [msg("p1", "text", text="привет")]}):
        await orch.ingest(m)
    row = db.get_chat("chat-1")
    check(
        "safe: незнакомый чат помечен старым",
        row and row["status"] == "existing",
        str(row.get("status") if row else "нет чата"),
    )
    check("safe: клиенту не ответили", w.sent == [], f"{w.sent}")
    check(
        "safe: роутинг всё равно сохранён (пригодится менеджеру)",
        bool(row and row.get("channel_id")),
    )
    await w.aclose()

    # свежий канал: там истории нет, чат берём
    fresh = Settings(**common, first_contact_policy="safe", fresh_channel_ids="ch-1")
    db2 = Database(tmp / "fc_fresh.db")
    w2 = SilentWazzup(fresh, db2)
    orch2 = Orchestrator(fresh, db2, w2)
    for m in w2.parse_webhook({"messages": [msg("p2", "text", text="привет")]}):
        await orch2.ingest(m)
    row2 = db2.get_chat("chat-1")
    check(
        "свежий канал: чат достаётся боту",
        row2 and row2["status"] == "new",
        str(row2.get("status") if row2 else "нет чата"),
    )
    await w2.aclose()

    # open: старое поведение доступно явным флагом
    op = Settings(**common, first_contact_policy="open")
    db3 = Database(tmp / "fc_open.db")
    w3 = SilentWazzup(op, db3)
    orch3 = Orchestrator(op, db3, w3)
    for m in w3.parse_webhook({"messages": [msg("p3", "text", text="привет")]}):
        await orch3.ingest(m)
    row3 = db3.get_chat("chat-1")
    check(
        "open: незнакомый чат считается новым",
        row3 and row3["status"] == "new",
        str(row3.get("status") if row3 else "нет чата"),
    )
    await w3.aclose()

    check("политика auto включена по умолчанию",
          Settings().first_contact_policy == "auto")

    # auto: пока история не загружена — молчим
    auto = Settings(**common, first_contact_policy="auto", baseline_min_chats=5)
    db4 = Database(tmp / "fc_auto_cold.db")
    w4 = SilentWazzup(auto, db4)
    orch4 = Orchestrator(auto, db4, w4)
    orch4.baseline_ready = False
    for m in w4.parse_webhook({"messages": [msg("p4", "text", text="привет")]}):
        await orch4.ingest(m)
    row4 = db4.get_chat("chat-1")
    check(
        "auto без загруженной истории: молчим",
        row4 and row4["status"] == "existing",
        str(row4.get("status") if row4 else "нет чата"),
    )
    check("auto без истории: клиенту не ответили", w4.sent == [])
    await w4.aclose()

    # auto: история загружена → «нет в базе» значит «новый», отвечаем
    db5 = Database(tmp / "fc_auto_warm.db")
    w5 = SilentWazzup(auto, db5)
    orch5 = Orchestrator(auto, db5, w5)
    orch5.baseline_ready = True
    for m in w5.parse_webhook({"messages": [msg("p5", "text", text="привет")]}):
        await orch5.ingest(m)
    row5 = db5.get_chat("chat-1")
    check(
        "auto с загруженной историей: чат достаётся боту",
        row5 and row5["status"] == "new",
        str(row5.get("status") if row5 else "нет чата"),
    )
    await w5.aclose()

    # baseline_ready поднимается по факту загрузки, а не по флагу в .env
    baseline_db = Database(tmp / "fc_ready.db")
    for i in range(6):
        baseline_db.upsert_chat(f"old-{i}", status="existing")
    w6 = SilentWazzup(auto, baseline_db)
    orch6 = Orchestrator(auto, baseline_db, w6)
    await orch6.startup()
    check(
        "старые чаты в базе с прошлого импорта → истории верим",
        orch6.baseline_ready,
        f"старых={baseline_db.count_chats_by_status('existing')}",
    )
    await orch6.shutdown()


async def test_max_reply_takes_over(tmp: Path) -> None:
    """Ответ владельца из MAX = перехват: дальше клиента ведёт он."""
    from app.notify.max import MaxNotifier

    local = Settings(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False,
        max_enabled=True, max_bot_token="t", max_group_id="1",
        poe_api_key="", vision_enabled=False,
    )

    class QuietMax(MaxNotifier):
        async def send(self, text, *, buttons=None):
            return "mid-1"

    db = Database(tmp / "takeover.db")
    w = SilentWazzup(local, db)
    relay = PriceRelay(db, w, QuietMax(local), local)

    db.upsert_chat("chat-1", status="new")
    rid = await relay.on_client_wants_price(
        chat_id="chat-1", summary="кухня", ask="посчитать"
    )
    check("заявка открыта", bool(rid))
    check(
        "до ответа владельца чат ещё за ботом",
        db.get_chat("chat-1")["status"] == "new",
    )

    ok = await relay.on_owner_max_message("Кухня — 145 000", request_id=rid)
    check("ответ владельца ушёл клиенту", ok)
    check(
        "после ответа владельца чат в manual — бот замолчал",
        db.get_chat("chat-1")["status"] == "manual",
        db.get_chat("chat-1")["status"],
    )
    check("перехват включён по умолчанию", Settings().max_reply_takes_over is True)

    # Перехват обратим: #старт возвращает бота в чат
    gate = Gatekeeper(db)
    gate.on_staff_message("chat-1", "#старт")
    check(
        "после #старт бот снова ведёт чат",
        db.get_chat("chat-1")["status"] == "new",
        db.get_chat("chat-1")["status"],
    )

    # А вот старый клиент из CRM не оживает никогда — это другой случай
    db.upsert_chat("old-1", status="existing")
    gate.on_staff_message("old-1", "#старт")
    check(
        "старого клиента из CRM #старт не оживляет",
        db.get_chat("old-1")["status"] == "existing",
        db.get_chat("old-1")["status"],
    )
    await w.aclose()


def test_baseline_keeps_live_dialog(db_path: Path) -> None:
    """Импорт из CRM не должен отбирать у бота живой диалог.

    Коннектор Wazzup заводит контакт в amoCRM на любое входящее — в том числе
    от нового клиента, которого бот только что взял. При следующем рестарте
    этот чат придёт в списке «старых»; если поверить списку вслепую, бот
    замолчит посреди разговора.
    """
    db = Database(db_path)
    gate = Gatekeeper(db)

    # клиент, которого бот уже ведёт
    db.upsert_chat("live-1", status="new")
    db.add_message("live-1", role="user", text="здравствуйте")
    db.add_message("live-1", role="assistant", text="Здравствуйте! Владимир.")

    # клиент, где бот отвечал, но строка чата потерялась (частичный бэкап)
    db.add_message("orphan-1", role="assistant", text="Здравствуйте! Владимир.")

    # действительно старый: ни статуса, ни реплик бота
    marked = gate.baseline_many(["live-1", "orphan-1", "really-old"])

    check("живой диалог не тронут", gate.status("live-1") == "new",
          gate.status("live-1") or "нет")
    check(
        "диалог без строки чата тоже уцелел (бот там говорил)",
        gate.status("orphan-1") != "existing",
        str(gate.status("orphan-1")),
    )
    check("настоящий старый помечен", gate.status("really-old") == "existing")
    check("помечен ровно один", marked == 1, f"marked={marked}")
    check(
        "bot_has_spoken различает живой и пустой чат",
        db.bot_has_spoken("live-1") and not db.bot_has_spoken("really-old"),
    )


def test_dialog_hygiene() -> None:
    """Баги из прошлых интеграций, которые нельзя оставлять на совесть модели.

    В присланных примерах бот здоровался в каждой реплике («Здравствуйте!
    хорошо, чтобы вы понимали...», «Здравствуйте! простите, конечно!»), иногда
    дважды подряд, и трижды повторил одну и ту же фразу про «напишите текстом».
    Промпт это запрещает, но запрет — не гарантия, поэтому режем в коде.
    """
    from app.services.poe import strip_banned_openers, strip_greeting

    cases = [
        ("Здравствуйте! добрый день! Хорошо, посчитаем.", "Хорошо, посчитаем."),
        ("Здравствуйте! хорошо, чтобы вы понимали порядок цифр",
         "Хорошо, чтобы вы понимали порядок цифр"),
        ("Добрый вечер! Кухня 3 метра — от 81 000.", "Кухня 3 метра — от 81 000."),
    ]
    for raw, want in cases:
        got = strip_greeting(raw)
        check(f"приветствие срезано: {raw[:34]!r}", got == want, got)

    check(
        "имя клиента после приветствия не теряется",
        strip_greeting("Добрый день, Игорь! Посчитаем.").startswith("Игорь"),
        strip_greeting("Добрый день, Игорь! Посчитаем."),
    )
    check(
        "реплика без приветствия не тронута",
        strip_greeting("Кухня от 81 000.") == "Кухня от 81 000.",
    )
    check(
        "сообщение из одного приветствия не превращается в пустое",
        strip_greeting("Здравствуйте!") == "Здравствуйте!",
    )
    check(
        "«Понял вас» по-прежнему срезается",
        not strip_banned_openers("Понял вас, посчитаем").lower().startswith("понял"),
        strip_banned_openers("Понял вас, посчитаем"),
    )

    # Три неразобранных голосовых подряд → одна подсказка, а не три копии
    from app.core.dialog import DialogService

    svc = DialogService.__new__(DialogService)
    same = "Клиент прислал голосовое, но разобрать речь не вышло."
    block = DialogService._context_block(
        svc, mode="продолжение", message="...", hints=[same, same, same],
    )
    check("одинаковые подсказки схлопнуты", block.count(same) == 1,
          f"повторов={block.count(same)}")

    # Факты о клиенте в служебный блок не дублируются: они уже сказаны в
    # переписке, а второй источник правды рано или поздно разойдётся с первым
    for banned in ("Что известно о клиенте", "НЕ СПРАШИВАЙ ЗАНОВО", "ЦЕЛЬ РАЗГОВОРА"):
        check(f"в служебном блоке нет «{banned}»", banned not in block)
    check(
        "в блоке осталось только нужное: режим, дата, имя, подсказки, сообщение",
        all(x in block for x in ("РЕЖИМ:", "Сегодня", "ТЕБЯ ЗОВУТ",
                                 "Подсказки системы:", "Сообщение клиента:")),
    )


async def test_chat_whitelist_and_reset(tmp: Path) -> None:
    """Тестовый режим: только свои chat_id, /start обнуляет чат.

    Канал уже отфильтрован, но на нём может оказаться и настоящий клиент —
    отвечать ему во время тестов нельзя. И /start должен давать чистый лист,
    иначе каждый прогон продолжает вчерашний разговор.
    """
    local = Settings(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=True,
        test_channel_ids="ch-1", fresh_channel_ids="ch-1",
        test_chat_ids="7936875555,1088968340",
        poe_api_key="", max_enabled=False, vision_enabled=False,
        backup_enabled=False, health_watch_enabled=False, fast_mode=True,
    )
    db = Database(tmp / "wl.db")
    w = SilentWazzup(local, db)
    orch = Orchestrator(local, db, w)
    orch.baseline_ready = True

    check("свой chat_id пропущен", w.chat_allowed("7936875555"))
    check("второй свой тоже", w.chat_allowed("1088968340"))
    check("чужой отсечён", not w.chat_allowed("79001234567"))

    # Чужой не должен даже завестись в базе
    for m in w.parse_webhook(
        {"messages": [msg("o1", "text", text="привет", chatId="79001234567")]}
    ):
        await orch.ingest(m)
    check("чужой чат в базу не попал", db.get_chat("79001234567") is None)
    check("чужому ничего не отправили", w.sent == [])

    # Свой — обрабатывается
    for m in w.parse_webhook(
        {"messages": [msg("m1", "text", text="нужна кухня", chatId="7936875555")]}
    ):
        await orch.ingest(m)
    check("свой чат заведён", db.get_chat("7936875555") is not None)

    # Накопим историю и заявку, потом /start должен всё стереть
    db.add_message("7936875555", role="assistant", text="Здравствуйте!")
    db.open_price_request(
        request_id="r1", chat_id="7936875555", summary="кухня", ask="посчитать"
    )
    before = len(db.recent_messages("7936875555", 50))
    check("история накопилась", before >= 2, f"сообщений={before}")

    for m in w.parse_webhook(
        {"messages": [msg("s1", "text", text="/start", chatId="7936875555")]}
    ):
        await orch.ingest(m)

    # Старое стёрто, но сам /start остаётся первой репликой новой переписки:
    # так бот увидит человека впервые и поздоровается, а не продолжит вчерашнее
    left = db.recent_messages("7936875555", 50)
    check(
        "/start стёр прошлую переписку",
        len(left) == 1 and left[0]["text"].strip() == "/start",
        f"осталось {len(left)}: {[m['text'][:20] for m in left]}",
    )
    check("/start закрыл заявку", db.get_price_by_request_id("r1") is None)
    check(
        "после /start чат снова первый контакт",
        (db.get_chat("7936875555") or {}).get("status") in (None, "new"),
        str((db.get_chat("7936875555") or {}).get("status")),
    )

    # В бою /start ничего не стирает
    prod = Settings(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False,
        poe_api_key="", max_enabled=False, vision_enabled=False,
        backup_enabled=False, health_watch_enabled=False, fast_mode=True,
    )
    db2 = Database(tmp / "prod.db")
    w2 = SilentWazzup(prod, db2)
    orch2 = Orchestrator(prod, db2, w2)
    orch2.baseline_ready = True
    db2.upsert_chat("chat-1", status="new")
    db2.add_message("chat-1", role="user", text="нужна кухня")
    for m in w2.parse_webhook({"messages": [msg("s2", "text", text="/start")]}):
        await orch2.ingest(m)
    check(
        "в бою /start историю НЕ трогает",
        len(db2.recent_messages("chat-1", 50)) >= 1,
        f"сообщений={len(db2.recent_messages('chat-1', 50))}",
    )

    await w.aclose()
    await w2.aclose()


async def test_no_double_greeting(tmp: Path) -> None:
    """Два батча из одного чата не должны здороваться оба.

    Живой случай: /start и «здравствуйте» пришли с разницей в 7 секунд, окно
    склейки 6 — получились два батча. Обрабатывались параллельно, и второй
    прочитал историю раньше, чем первый успел записать свой ответ (тот
    сохраняется только после отправки, а перед ней пауза 5-8 секунд). Второй
    решил, что он первый контакт, и клиент получил два приветствия подряд.
    """
    local = Settings(
        wazzup_api_key="k", wazzup_send_enabled=False, test_mode=False,
        poe_api_key="", max_enabled=False, vision_enabled=False,
        backup_enabled=False, health_watch_enabled=False, fast_mode=True,
    )
    db = Database(tmp / "race.db")
    w = SilentWazzup(local, db)
    orch = Orchestrator(local, db, w)
    orch.baseline_ready = True
    db.upsert_chat("chat-1", status="new")

    order: list[str] = []

    async def slow_handle(chat_id, messages):
        """Медленный обработчик: держит чат дольше, чем приходит второй батч."""
        order.append(f"start:{messages[0].text}")
        await asyncio.sleep(0.05)
        # Ответ бота попадает в историю только здесь — как в бою, после отправки
        db.add_message(chat_id, role="assistant", text="Здравствуйте! Владимир.")
        order.append(f"end:{messages[0].text}")

    orch._handle_batch = slow_handle  # noqa: SLF001

    m1 = w.parse_webhook({"messages": [msg("b1", "text", text="/start")]})[0]
    m2 = w.parse_webhook({"messages": [msg("b2", "text", text="здравствуйте")]})[0]

    # Оба батча стартуют одновременно — ровно как в проде
    await asyncio.gather(
        orch._on_batch("chat-1", [m1]),  # noqa: SLF001
        orch._on_batch("chat-1", [m2]),  # noqa: SLF001
    )

    check(
        "батчи одного чата не перекрываются",
        order in (
            ["start:/start", "end:/start", "start:здравствуйте", "end:здравствуйте"],
            ["start:здравствуйте", "end:здравствуйте", "start:/start", "end:/start"],
        ),
        " → ".join(order),
    )
    check(
        "блокировки не копятся после обработки",
        orch._chat_locks == {},  # noqa: SLF001
        f"{list(orch._chat_locks)}",  # noqa: SLF001
    )

    # Разные чаты друг друга не ждут — иначе один медленный клиент
    # затормозил бы всю очередь
    db.upsert_chat("chat-2", status="new")
    order.clear()
    await asyncio.gather(
        orch._on_batch("chat-1", [m1]),  # noqa: SLF001
        orch._on_batch("chat-2", [m2]),  # noqa: SLF001
    )
    check(
        "разные чаты обрабатываются параллельно",
        order[0].startswith("start:") and order[1].startswith("start:"),
        " → ".join(order),
    )
    await w.aclose()


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
        # Канал теста объявлен свежим — иначе политика safe (умолчание)
        # пометит незнакомый чат старым, и до пайплайна дело не дойдёт.
        # В бою так же настроен наш телеграм-бот.
        fresh_channel_ids="ch-1",
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

        print("\n--- Один чат обрабатывается по очереди ---")
        asyncio.run(test_no_double_greeting(tmp))

        print("\n--- Тестовый режим: свои chat_id и сброс по /start ---")
        asyncio.run(test_chat_whitelist_and_reset(tmp))

        print("\n--- Бот берёт только новых клиентов ---")
        asyncio.run(test_first_contact_policy(tmp))

        print("\n--- Ответ владельца из MAX = перехват ---")
        asyncio.run(test_max_reply_takes_over(tmp))

        print("\n--- amoCRM: разбор полей и исключения ---")
        asyncio.run(test_amocrm_extraction(tmp))

        print("\n--- Гигиена диалога: приветствия и повторы ---")
        test_dialog_hygiene()

        print("\n--- Импорт из CRM не отбирает живой диалог ---")
        test_baseline_keeps_live_dialog(tmp / "live.db")

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
