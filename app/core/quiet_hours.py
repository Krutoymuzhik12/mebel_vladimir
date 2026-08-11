"""Тихие часы и политика Push/Pull.

Pull (ответ на входящее) — всегда.
Push (сам пишем клиенту) — только push_hour_start .. push_hour_end по Москве.
По умолчанию: 09:00–18:00 МСК (тихое время 18:00→09:00).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings


class QuietHours:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        try:
            self.tz = ZoneInfo(settings.timezone)
        except Exception:
            # Windows без tzdata — фиксированный UTC+3 как запасной вариант для Москвы
            from datetime import timedelta, timezone

            self.tz = timezone(timedelta(hours=3), name="MSK-fallback")

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def in_push_window(self, when: datetime | None = None) -> bool:
        local = when or self.now()
        hour = local.hour
        start = self.settings.push_hour_start
        end = self.settings.push_hour_end
        return start <= hour < end

    def can_pull(self) -> bool:
        return True

    def can_push(self) -> bool:
        return self.in_push_window()
