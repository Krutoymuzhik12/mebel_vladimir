"""SQLite: статусы чатов, история (40), таймеры, дедуп."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CHAT_COLUMNS = frozenset(
    {
        "status",
        "channel",
        # Роутинг ответа в Wazzup: без них отправить сообщение нельзя
        "channel_id",
        "chat_type",
        # Накопленные факты о клиенте (JSON)
        "facts",
        "last_user_msg_at",
        "last_bot_msg_at",
        # Интент последнего сообщения клиента — по нему выбираем дожим
        "last_intent",
        "followup_stage",
        "followup_last_sent_at",
        # Клиент попросил вернуться в конкретный срок
        "followup_due_at",
        "snooze_reason",
        "show_phone_at",
        "show_phone_notified",
    }
)

# Колонки, добавленные после первой версии схемы — доливаем ALTER-ом
_MIGRATIONS = (
    ("chats", "channel_id", "TEXT"),
    ("chats", "chat_type", "TEXT"),
    ("chats", "facts", "TEXT"),
    ("chats", "last_intent", "TEXT"),
    ("price_requests", "reminder_sent_at", "TEXT"),
    # mid карточки в MAX — по нему ловим ответ владельца реплаем
    ("price_requests", "max_message_id", "TEXT"),
    ("chats", "followup_due_at", "TEXT"),
    ("chats", "snooze_reason", "TEXT"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    channel TEXT,
                    last_user_msg_at TEXT,
                    last_bot_msg_at TEXT,
                    followup_stage INTEGER NOT NULL DEFAULT 0,
                    followup_last_sent_at TEXT,
                    show_phone_at TEXT,
                    show_phone_notified INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_messages (
                    message_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'text',
                    external_id TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS price_requests (
                    request_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    ask TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    closed_at TEXT
                );

                -- Кто из сотрудников MAX сейчас пишет ответ и на какую заявку.
                -- Ключ — сотрудник, а не заявка: пока он печатает, новые
                -- заявки могут приходить сколько угодно и его не сбивают.
                CREATE TABLE IF NOT EXISTS max_awaiting (
                    max_chat_id TEXT NOT NULL,
                    max_user_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (max_chat_id, max_user_id)
                );

                -- Клиент прислал документ (PDF/Word/др.) — не разбираем содержимое,
                -- пересылаем владельцу ссылкой и ждём его ответ клиенту. Своя
                -- таблица, а не price_requests: документов в чате может быть
                -- несколько подряд, каждый — своя карточка, без dedup по чату.
                CREATE TABLE IF NOT EXISTS document_requests (
                    request_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    doc_url TEXT NOT NULL DEFAULT '',
                    doc_name TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    max_message_id TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_chats_status ON chats(status);
                CREATE INDEX IF NOT EXISTS idx_chats_followup ON chats(status, followup_stage);
                CREATE INDEX IF NOT EXISTS idx_price_pending
                    ON price_requests(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_document_pending
                    ON document_requests(chat_id, status);
                CREATE INDEX IF NOT EXISTS idx_messages_chat
                    ON messages(chat_id, id);
                """
            )

    def _migrate(self) -> None:
        """Доливает колонки, появившиеся после первой версии схемы."""
        with self._conn() as conn:
            for table, column, coltype in _MIGRATIONS:
                existing = {
                    r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
                }
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                    logger.info("migration: %s.%s added", table, column)

    def get_chat(self, chat_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return dict(row) if row else None

    def upsert_chat(self, chat_id: str, **fields: Any) -> None:
        unknown = set(fields) - CHAT_COLUMNS
        if unknown:
            raise ValueError(f"unknown chat columns: {sorted(unknown)}")

        now = _utc_now()
        existing = self.get_chat(chat_id)
        if existing is None:
            status = fields.get("status", "new")
            channel = fields.get("channel")
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO chats (chat_id, status, channel, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, status, channel, now, now),
                )
            extra = {k: v for k, v in fields.items() if k not in {"status", "channel"}}
            if extra:
                self.upsert_chat(chat_id, **extra)
            return

        cols: list[str] = []
        vals: list[Any] = []
        for key, value in fields.items():
            cols.append(f"{key}=?")
            vals.append(value)
        cols.append("updated_at=?")
        vals.append(now)
        vals.append(chat_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE chats SET {', '.join(cols)} WHERE chat_id=?",
                vals,
            )

    def seen_message(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        return row is not None

    def mark_seen(self, message_id: str, chat_id: str) -> None:
        if not message_id:
            return
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO seen_messages (message_id, chat_id, seen_at)
                VALUES (?, ?, ?)
                """,
                (message_id, chat_id, _utc_now()),
            )

    def add_message(
        self,
        chat_id: str,
        *,
        role: str,
        text: str = "",
        kind: str = "text",
        external_id: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO messages (chat_id, role, text, kind, external_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, role, text or "", kind, external_id, _utc_now()),
            )

    def recent_messages(self, chat_id: str, limit: int = 40) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT role, text, kind, external_id, created_at FROM messages
                WHERE chat_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def remember_route(
        self, chat_id: str, *, channel_id: str, chat_type: str, channel: str = ""
    ) -> None:
        """Куда отвечать: без channel_id/chat_type Wazzup сообщение не примет."""
        if not channel_id and not chat_type:
            return
        fields: dict[str, Any] = {}
        if channel_id:
            fields["channel_id"] = channel_id
        if chat_type:
            fields["chat_type"] = chat_type
            fields["channel"] = channel or chat_type
        self.upsert_chat(chat_id, **fields)

    def get_route(self, chat_id: str) -> tuple[str, str]:
        row = self.get_chat(chat_id)
        if not row:
            return "", ""
        return (row.get("channel_id") or "", row.get("chat_type") or "")

    def get_facts(self, chat_id: str) -> dict[str, Any]:
        row = self.get_chat(chat_id)
        if not row or not row.get("facts"):
            return {}
        try:
            data = json.loads(row["facts"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def merge_facts(self, chat_id: str, new_facts: dict[str, Any]) -> dict[str, Any]:
        """Копим факты о клиенте между сообщениями: новые непустые перетирают старые."""
        clean = {k: v for k, v in (new_facts or {}).items() if v not in (None, "", [])}
        if not clean:
            return self.get_facts(chat_id)
        merged = {**self.get_facts(chat_id), **clean}
        self.upsert_chat(chat_id, facts=json.dumps(merged, ensure_ascii=False))
        return merged

    def touch_user_message(self, chat_id: str) -> None:
        self.upsert_chat(
            chat_id,
            last_user_msg_at=_utc_now(),
            followup_stage=0,
        )

    def touch_bot_message(self, chat_id: str) -> None:
        self.upsert_chat(chat_id, last_bot_msg_at=_utc_now())

    def candidates_for_followup(self, max_stage: int = 3) -> list[dict[str, Any]]:
        """Чаты, где клиент молчит после нашего сообщения.

        Насколько долго он должен молчать — здесь не решаем: задержка зависит от
        причины срыва (см. app/core/stall.py), поэтому отдаём всех молчащих
        вместе с посчитанным silent_hours, а отбор делает вызывающий.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chats
                WHERE status='new'
                  AND followup_stage < ?
                  AND last_bot_msg_at IS NOT NULL
                """,
                (max_stage,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for row in rows:
            d = dict(row)
            bot_at = _parse_iso(d.get("last_bot_msg_at"))
            user_at = _parse_iso(d.get("last_user_msg_at"))
            if bot_at is None:
                continue
            # клиент ответил после бота — не пушим
            if user_at is not None and user_at >= bot_at:
                continue
            d["silent_hours"] = (now - bot_at).total_seconds() / 3600.0
            out.append(d)
        return out

    def stale_pending_prices(self, older_than_hours: float) -> list[dict[str, Any]]:
        """Заявки на расчёт, которые владелец не закрыл за отведённое время.

        Клиент в таких чатах ждёт нас — вместо дожима клиенту напоминаем себе.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM price_requests WHERE status='pending'"
            ).fetchall()
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(row)
            created = _parse_iso(d.get("created_at"))
            if created is None:
                continue
            age_h = (now - created).total_seconds() / 3600.0
            if age_h >= older_than_hours:
                d["age_hours"] = age_h
                out.append(d)
        return out

    def set_message_text(self, external_id: str, text: str) -> None:
        """Дописать текст к уже сохранённому сообщению.

        Нужно для голосовых: в момент приёма текста ещё нет, он появляется
        после расшифровки, а в историю диалога должна попасть именно речь.
        """
        if not external_id:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE messages SET text=? WHERE external_id=?",
                (text, external_id),
            )

    def price_reminder_sent(self, request_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT reminder_sent_at FROM price_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return bool(row and row["reminder_sent_at"])

    def mark_price_reminder_sent(self, request_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE price_requests SET reminder_sent_at=? WHERE request_id=?",
                (_utc_now(), request_id),
            )

    def snooze_chat(self, chat_id: str, days: int, reason: str = "") -> str:
        """Клиент попросил вернуться через N дней.

        Ступени дожима сбрасываем: договорённость о сроке отменяет лестницу,
        которая шла до неё. Иначе человек, попросивший «напишите через
        неделю», получит дежурное напоминание уже завтра.
        """
        due = datetime.now(timezone.utc) + timedelta(days=max(1, days))
        self.upsert_chat(
            chat_id,
            followup_due_at=due.isoformat(),
            snooze_reason=(reason or "")[:200],
            followup_stage=0,
        )
        return due.isoformat()

    def clear_snooze(self, chat_id: str) -> None:
        self.upsert_chat(chat_id, followup_due_at=None, snooze_reason=None)

    def record_followup_sent(self, chat_id: str, stage: int = 1) -> None:
        self.upsert_chat(
            chat_id,
            followup_stage=stage,
            followup_last_sent_at=_utc_now(),
            last_bot_msg_at=_utc_now(),
        )

    def candidates_show_phone(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM chats
                WHERE show_phone_at IS NOT NULL
                  AND show_phone_notified = 0
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_show_phone(self, chat_id: str) -> None:
        self.upsert_chat(chat_id, show_phone_at=_utc_now(), show_phone_notified=0)

    def mark_show_phone_notified(self, chat_id: str) -> None:
        self.upsert_chat(chat_id, show_phone_notified=1)

    def count_pending_prices(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM price_requests WHERE status='pending'"
            ).fetchone()
        return int(row["n"] if row else 0)

    def latest_pending_price(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM price_requests
                WHERE status='pending'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_pending_price(self, chat_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM price_requests
                WHERE chat_id=? AND status='pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_price_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM price_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def open_price_request(
        self,
        *,
        request_id: str,
        chat_id: str,
        summary: str,
        ask: str,
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO price_requests
                    (request_id, chat_id, summary, ask, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (request_id, chat_id, summary, ask, now),
            )

    def set_price_max_message(self, request_id: str, mid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE price_requests SET max_message_id=? WHERE request_id=?",
                (mid, request_id),
            )

    def get_price_by_max_message(self, mid: str) -> dict[str, Any] | None:
        """Заявка по карточке в MAX — владелец ответил на неё реплаем."""
        if not mid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM price_requests WHERE max_message_id=?",
                (mid,),
            ).fetchone()
        return dict(row) if row else None

    def skip_price_request(self, request_id: str) -> None:
        """Владелец нажал «Пропустить»: клиент возвращается в обычные дожимы."""
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE price_requests
                SET status='skipped', closed_at=?
                WHERE request_id=? AND status='pending'
                """,
                (_utc_now(), request_id),
            )

    # ---------- ожидание ответа сотрудника в MAX ----------

    def set_awaiting(self, max_chat_id: str, max_user_id: str, request_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO max_awaiting (max_chat_id, max_user_id, request_id, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(max_chat_id, max_user_id) DO UPDATE SET
                    request_id=excluded.request_id,
                    created_at=excluded.created_at
                """,
                (str(max_chat_id), str(max_user_id), request_id, _utc_now()),
            )

    def get_awaiting(
        self, max_chat_id: str, max_user_id: str, *, ttl_minutes: float = 60.0
    ) -> str | None:
        """Заявка, ответ на которую сотрудник сейчас печатает.

        Протухшее ожидание не отдаём: иначе забытое нажатие «Ответить» через
        сутки перехватит постороннюю реплику в чате и отправит её клиенту.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT request_id, created_at FROM max_awaiting
                WHERE max_chat_id=? AND max_user_id=?
                """,
                (str(max_chat_id), str(max_user_id)),
            ).fetchone()
        if not row:
            return None
        created = _parse_iso(row["created_at"])
        if created is None:
            return None
        age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60.0
        if age_min > ttl_minutes:
            self.clear_awaiting(max_chat_id, max_user_id)
            return None
        return str(row["request_id"])

    def clear_awaiting(self, max_chat_id: str, max_user_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM max_awaiting WHERE max_chat_id=? AND max_user_id=?",
                (str(max_chat_id), str(max_user_id)),
            )

    def clear_awaiting_for_request(self, request_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM max_awaiting WHERE request_id=?", (request_id,)
            )

    def close_price_request(self, request_id: str, *, delivered: bool) -> None:
        status = "delivered" if delivered else "cancelled"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE price_requests
                SET status=?, closed_at=?
                WHERE request_id=?
                """,
                (status, _utc_now(), request_id),
            )

    # ---------- документы: пересылка владельцу без разбора содержимого ----------

    def open_document_request(
        self,
        *,
        request_id: str,
        chat_id: str,
        doc_url: str,
        doc_name: str,
        note: str = "",
    ) -> None:
        now = _utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO document_requests
                    (request_id, chat_id, doc_url, doc_name, note, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (request_id, chat_id, doc_url, doc_name, note, now),
            )

    def set_document_max_message(self, request_id: str, mid: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE document_requests SET max_message_id=? WHERE request_id=?",
                (mid, request_id),
            )

    def get_document_by_max_message(self, mid: str) -> dict[str, Any] | None:
        if not mid:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM document_requests WHERE max_message_id=?",
                (mid,),
            ).fetchone()
        return dict(row) if row else None

    def get_document_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM document_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_pending_documents(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM document_requests WHERE status='pending'"
            ).fetchone()
        return int(row["n"] if row else 0)

    def latest_pending_document(self) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM document_requests
                WHERE status='pending'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def skip_document_request(self, request_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE document_requests
                SET status='skipped', closed_at=?
                WHERE request_id=? AND status='pending'
                """,
                (_utc_now(), request_id),
            )

    def close_document_request(self, request_id: str, *, delivered: bool) -> None:
        status = "delivered" if delivered else "cancelled"
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE document_requests
                SET status=?, closed_at=?
                WHERE request_id=?
                """,
                (status, _utc_now(), request_id),
            )

    def count_chats_by_status(self, status: str) -> int:
        """Сколько чатов в этом статусе.

        Нужно, чтобы понять, загружен ли baseline: если в базе уже лежат
        тысячи «старых» чатов с прошлого успешного импорта, значит истории
        мы знаем и незнакомому чату можно доверять как новому.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chats WHERE status=?", (status,)
            ).fetchone()
        return int(row["n"] if row else 0)

    def bot_has_spoken(self, chat_id: str) -> bool:
        """Бот уже отвечал в этом чате?

        Второй, независимый от статуса признак «диалог наш». Нужен на случай,
        когда строка chats потерялась (частичное восстановление базы), а
        переписка осталась: без него импорт из CRM пометил бы живой диалог
        старым и бот замолчал бы посреди разговора.
        """
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM messages
                WHERE chat_id=? AND role='assistant'
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        return row is not None

    def forget_chat(self, chat_id: str) -> dict[str, int]:
        """Стереть чат из базы целиком: статус, историю, дедуп, заявки.

        Нужно для тестов: после /start бот должен увидеть человека впервые,
        а не продолжать вчерашний разговор. Возвращает, сколько строк удалено
        из каждой таблицы — чтобы в логе было видно, что реально стёрлось.
        """
        counts: dict[str, int] = {}
        with self._conn() as conn:
            for table in (
                "messages",
                "seen_messages",
                "price_requests",
                "document_requests",
                "chats",
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE chat_id=?", (chat_id,))
                counts[table] = cur.rowcount
        return counts
