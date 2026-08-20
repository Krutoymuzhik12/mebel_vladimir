"""Фоновый монитор: Poe / диск / БД. Алерт в MAX при деградации."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.notify.max import MaxNotifier

logger = logging.getLogger(__name__)


class HealthWatch:
    def __init__(self, settings: Settings, max_notifier: MaxNotifier) -> None:
        self.settings = settings
        self.max = max_notifier
        self._last_alert_at = 0.0
        self._last_ok = True

    async def check(self) -> dict[str, Any]:
        issues: list[str] = []
        poe_ok = await self._poe_ok()
        if not poe_ok:
            issues.append("Poe недоступен или ключ битый")

        db = self.settings.db_file
        if not db.parent.exists():
            issues.append(f"нет каталога БД: {db.parent}")
        else:
            try:
                probe = db.parent / ".health_write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
            except OSError:
                issues.append(f"не пишется диск: {db.parent}")

        if db.is_file() and db.stat().st_size == 0:
            issues.append("bot.db пустой (0 байт)")

        ok = not issues
        status = {"ok": ok, "issues": issues, "poe_ok": poe_ok}
        if ok:
            if not self._last_ok:
                logger.info("health: сервис снова в норме")
            self._last_ok = True
            return status

        self._last_ok = False
        logger.warning("health: проблемы — %s", "; ".join(issues))
        await self._maybe_alert(issues)
        return status

    async def _poe_ok(self) -> bool:
        key = (self.settings.poe_api_key or "").strip()
        if not key:
            return False
        # Лёгкий запрос: список моделей / models — без генерации ответа клиенту
        url = self.settings.poe_base_url.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {key}"}
                )
            if resp.status_code in (200, 401, 403):
                # 401/403 = ключ дошёл до API, но отказ — это «ключ мёртв»
                return resp.status_code == 200
            logger.warning("health poe http=%s", resp.status_code)
            return False
        except httpx.HTTPError:
            logger.exception("health: Poe ping failed")
            return False

    async def _maybe_alert(self, issues: list[str]) -> None:
        # Владельца в MAX сейчас беспокоим только расчётами и файлами.
        # Проблемы сервиса видны в логе и в /health — туда и смотрим.
        if not self.settings.max_notify_health:
            return
        # Не чаще раза в час, чтобы не спамить MAX
        now = time.monotonic()
        if now - self._last_alert_at < 3600:
            return
        self._last_alert_at = now
        text = "⚠️ Бот: проблемы\n" + "\n".join(f"• {i}" for i in issues)
        await self.max.send(text)
