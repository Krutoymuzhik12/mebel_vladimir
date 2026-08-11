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
    push_hour_start: int = 9
    push_hour_end: int = 18
    followup_silence_hours: float = 4.0

    wazzup_api_key: str = ""
    wazzup_webhook_secret: str = ""
    wazzup_api_base: str = "https://api.wazzup24.com"

    poe_api_key: str = ""
    poe_base_url: str = "https://api.poe.com/v1"
    poe_manager_bot: str = ""
    poe_classifier_bot: str = ""
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

    @property
    def db_file(self) -> Path:
        path = Path(self.db_path)
        return path if path.is_absolute() else ROOT / path

    @property
    def log_file(self) -> Path:
        path = Path(self.log_path)
        return path if path.is_absolute() else ROOT / path


settings = Settings()
