"""Настройки из .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    fast_mode: bool = False
    timezone: str = "Europe/Moscow"
    # 8080 на сервере занят — порт вынесен в .env
    app_host: str = "127.0.0.1"
    app_port: int = 8095
    push_hour_start: int = 9
    push_hour_end: int = 19
    followup_silence_hours: float = 4.0

    wazzup_api_key: str = ""
    wazzup_webhook_secret: str = ""
    wazzup_api_base: str = "https://api.wazzup24.com"
    # Публичный URL сервиса — нужен, чтобы зарегистрировать webhook в Wazzup
    public_webhook_url: str = ""
    # Ретраи отправки в Wazzup (сеть / 429 / 5xx)
    wazzup_send_retries: int = 3
    wazzup_send_retry_backoff_sec: float = 1.5

    # ---------- amoCRM (baseline existing) ----------
    # Пока токена нет — только файл data/existing_baseline.txt
    amocrm_base_url: str = ""
    amocrm_token: str = ""
    amocrm_baseline_file: str = "data/existing_baseline.txt"
    # Чаты, которые НИКОГДА не помечаем «старыми», даже если они есть в CRM.
    # Нужно для тестовых: наш телеграм уже завёл контакт в amoCRM, и после
    # пересоздания базы бот замолчал бы в собственном тестовом чате.
    baseline_exclude_chat_ids: str = ""
    # Незнакомый чат: safe — считаем его старым и молчим; open — считаем
    # первым контактом и отвечаем. Отличить по данным Wazzup невозможно:
    # ни списка чатов, ни истории он не отдаёт. Дефолт safe намеренно —
    # промолчать новому клиенту дешевле, чем поздороваться со старым.
    first_contact_policy: str = "safe"
    # UUID каналов, где истории до бота нет по определению (свежий бот,
    # тестовый канал) — там незнакомый чат безопасно считать новым.
    fresh_channel_ids: str = ""
    # Ответ владельца из MAX = перехват: дальше он ведёт клиента сам
    max_reply_takes_over: bool = True

    # ---------- Тестовый режим ----------
    # 1 — слушаем только каналы из test_channel_ids / test_chat_types
    test_mode: bool = False
    # UUID каналов Wazzup через запятую (узнать: python -m scripts.wazzup_channels)
    test_channel_ids: str = ""
    # Типы чатов через запятую: telegram, whatsapp, avito, instagram, vk, max
    test_chat_types: str = ""
    # Логировать сырой payload вебхука целиком (для разбора форматов)
    log_raw_webhook: bool = False
    # 0 — «сухой» прогон: считаем ответ, но не отправляем клиенту.
    # Дефолт False: забытая переменная в .env не должна приводить к письмам клиентам.
    wazzup_send_enabled: bool = False
    # Менеджер ответил руками (isEcho) → чат уходит в manual
    staff_takeover_on_echo: bool = True
    # Перехватывать чат только если у эха есть автор. Пустой автор — это
    # автоответ площадки или другая интеграция, а не живой менеджер.
    staff_takeover_requires_author: bool = True

    poe_api_key: str = ""
    poe_base_url: str = "https://api.poe.com/v1"
    poe_manager_bot: str = "Vladimir_dialog"
    poe_classifier_bot: str = "Vladimir_Intent"
    # Промпты живут в самих ботах на Poe, а не в коде. Отсюда уходит только
    # история переписки, факты о клиенте и вложения.
    # Файлы в prompts/ — исходники, которые вставляются в ботов вручную.
    send_system_prompts: bool = False
    history_limit: int = 40
    confidence_threshold: float = 0.6

    # MAX Bot API: токен бота и id рабочего чата, куда падают заявки
    max_bot_token: str = ""
    max_group_id: str = ""
    max_enabled: bool = False
    # Сколько минут держим режим «сотрудник печатает ответ» после нажатия кнопки
    max_awaiting_ttl_min: float = 60.0
    # Чем MAX вправе беспокоить владельца: расчёты, файлы и Авито «показал
    # номер без звонка» — по такому событию владелец звонит сам, поэтому оно
    # включено. Алерты мониторинга остаются в логе, чтобы не размывать поток.
    max_notify_avito: bool = True
    max_notify_health: bool = False

    company_name: str = ""
    manager_name: str = "Владимир"

    reply_delay_min_sec: float = 5.0
    reply_delay_max_sec: float = 8.0
    message_batch_wait_sec: float = 6.0
    message_batch_settle_sec: float = 0.8
    message_batch_tail_wait_sec: float = 4.0
    message_batch_max_wait_sec: float = 30.0
    # Пауза после фото/голосового — клиент часто дописывает условие следом
    message_batch_media_wait_sec: float = 12.0

    transcription_provider: str = "auto"
    whisper_model: str = "small"
    openai_api_key: str = ""
    poe_whisper_bot: str = "Gemini-2.5-Flash"
    # Больше 25 МБ голосовых не бывает, а качать чужие ссылки без предела нельзя
    media_max_bytes: int = 26_214_400
    # Потолок на расшифровку: длинное голосовое → отказ, просим текстом
    voice_max_seconds: float = 90.0
    voice_max_bytes: int = 3_000_000

    # Бэкап SQLite и монитор
    backup_enabled: bool = True
    backup_dir: str = "data/backups"
    backup_keep: int = 14
    backup_interval_hours: float = 24.0
    health_watch_enabled: bool = True
    health_watch_interval_sec: float = 600.0
    max_inbox_lock_path: str = "data/max_inbox.lock"

    # Показывать клиенту фото найденных позиций каталога
    send_catalog_photos: bool = True
    catalog_photos_limit: int = 3

    vision_enabled: bool = False
    vision_api_url: str = "http://127.0.0.1:8090"
    vision_top_k: int = 5
    similarity_threshold: float = 0.42
    cut_bg: bool = True
    cut_bg_on_index: bool = False
    qdrant_mode: str = "local"
    qdrant_path: str = "data/qdrant_local"
    qdrant_collection: str = "vasha_mebel"
    catalog_path: str = "catalog"

    db_path: str = "data/bot.db"
    log_path: str = "data/bot.log"

    @staticmethod
    def _csv_set(raw: str) -> set[str]:
        return {part.strip().lower() for part in (raw or "").split(",") if part.strip()}

    @property
    def test_channel_id_set(self) -> set[str]:
        return self._csv_set(self.test_channel_ids)

    @property
    def test_chat_type_set(self) -> set[str]:
        return self._csv_set(self.test_chat_types)

    @property
    def baseline_exclude_set(self) -> set[str]:
        return self._csv_set(self.baseline_exclude_chat_ids)

    @property
    def fresh_channel_id_set(self) -> set[str]:
        return self._csv_set(self.fresh_channel_ids)

    def public_media_url(self, rel_path: str) -> str:
        """Ссылка на файл каталога, которую примет Wazzup.

        Wazzup скачивает вложение сам, поэтому путь на диске ему не годится —
        нужен публичный https. Каталог раздаёт тот же nginx, что и вебхук,
        так что достаточно приклеить относительный путь к нашему домену.
        """
        base = (self.public_webhook_url or "").rstrip("/")
        rel = (rel_path or "").replace("\\", "/").lstrip("/")
        if not base or not rel:
            return ""
        return f"{base}/{rel}"

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def log_file(self) -> Path:
        path = Path(self.log_path)
        return path if path.is_absolute() else ROOT / path


settings = Settings()
