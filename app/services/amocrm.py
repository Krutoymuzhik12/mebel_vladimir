"""amoCRM: baseline «уже были в переписке» → status=existing.

Пока API-ключа нет — работаем на заглушке. Когда появятся AMOCRM_BASE_URL +
AMOCRM_TOKEN, здесь же подставим реальный GET /api/v4/contacts (и при необходимости
сделки). Дополнительно всегда читаем локальный файл data/existing_baseline.txt —
удобно руками докинуть chat_id / телефон, пока интеграция не готова.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import ROOT, Settings

logger = logging.getLogger(__name__)

DEFAULT_BASELINE_FILE = ROOT / "data" / "existing_baseline.txt"


def _from_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.split("#", 1)[0].strip()
        if raw:
            out.append(raw)
    return out


async def _from_amocrm_api(settings: Settings) -> list[str]:
    """Реальный вызов — когда токен появится. Сейчас почти всегда пусто."""
    base = (settings.amocrm_base_url or "").rstrip("/")
    token = (settings.amocrm_token or "").strip()
    if not base or not token or token.startswith("REPLACE_"):
        return []

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ids: list[str] = []
    page = 1
    async with httpx.AsyncClient(timeout=30.0) as client:
        while page <= 50:  # потолок на всякий случай
            resp = await client.get(
                f"{base}/api/v4/contacts",
                headers=headers,
                params={"limit": 250, "page": page},
            )
            if resp.status_code == 401:
                logger.error("amoCRM: 401 — проверьте AMOCRM_TOKEN")
                break
            if resp.status_code >= 400:
                logger.error("amoCRM contacts http=%s %s", resp.status_code, resp.text[:200])
                break
            data = resp.json() if resp.content else {}
            embedded = (data.get("_embedded") or {}) if isinstance(data, dict) else {}
            contacts = embedded.get("contacts") or []
            if not contacts:
                break
            for c in contacts:
                if not isinstance(c, dict):
                    continue
                cid = str(c.get("id") or "").strip()
                if cid:
                    ids.append(cid)
                # телефоны / chat-ключи из custom fields — уточним по живому ответу
                for field in (c.get("custom_fields_values") or []):
                    if not isinstance(field, dict):
                        continue
                    for val in field.get("values") or []:
                        if isinstance(val, dict) and val.get("value"):
                            ids.append(str(val["value"]).strip())
            page += 1
    return ids


async def baseline_existing_ids(settings: Settings) -> list[str]:
    """Список ключей чатов/контактов, которых бот не должен трогать.

    Источники (мержим):
    1. data/existing_baseline.txt (или AMOCRM_BASELINE_FILE)
    2. amoCRM API, если задан токен (иначе — молча пропускаем)
    """
    path = Path(settings.amocrm_baseline_file or DEFAULT_BASELINE_FILE)
    if not path.is_absolute():
        path = ROOT / path

    from_file = _from_file(path)
    from_api: list[str] = []
    try:
        from_api = await _from_amocrm_api(settings)
    except Exception:
        logger.exception("amoCRM baseline: API недоступен, берём только файл")

    if not from_api and not (settings.amocrm_token or "").strip():
        logger.info(
            "amoCRM: токен не задан — baseline только из файла (%s шт.). "
            "Позже подставим AMOCRM_TOKEN.",
            len(from_file),
        )
    elif from_api:
        logger.info("amoCRM API: получено %s ключей", len(from_api))

    # уникальные, порядок стабильный
    seen: set[str] = set()
    out: list[str] = []
    for item in from_file + from_api:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def probe_config(settings: Settings) -> dict[str, Any]:
    """Для /health — без секретов."""
    token = (settings.amocrm_token or "").strip()
    return {
        "configured": bool(
            (settings.amocrm_base_url or "").strip()
            and token
            and not token.startswith("REPLACE_")
        ),
        "baseline_file": settings.amocrm_baseline_file,
        "stub": not bool(token) or token.startswith("REPLACE_"),
    }
