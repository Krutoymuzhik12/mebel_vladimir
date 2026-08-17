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
    push_hour_end: int = 18
    followup_silence_hours: float = 4.0

    wazzup_api_key: str = ""
    wazzup_webhook_secret: str = ""
    wazzup_api_base: str = "https://api.wazzup24.com"
    # Публичный URL сервиса — нужен, чтобы зарегистрировать webhook в Wazzup
    public_webhook_url: str = ""

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

    poe_api_key: str = ""
    poe_base_url: str = "https://api.poe.com/v1"
    poe_manager_bot: str = "Vladimir_dialog"
    poe_classifier_bot: str = "Vladimir_Intent"
    send_system_prompts: bool = False
    history_limit: int = 40
    confidence_threshold: float = 0.6

    max_group_id: str = ""
    max_enabled: bool = False

    company_name: str = ""
    manager_name: str = "Владимир"

    reply_delay_min_sec: float = 5.0
    reply_delay_max_sec: float = 8.0
    message_batch_wait_sec: float = 6.0
    message_batch_settle_sec: float = 0.8
    message_batch_tail_wait_sec: float = 4.0
    message_batch_max_wait_sec: float = 30.0

    transcription_provider: str = "auto"
    whisper_model: str = "small"
    openai_api_key: str = ""

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
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def log_file(self) -> Path:
        path = Path(self.log_path)
        return path if path.is_absolute() else ROOT / path


settings = Settings()
