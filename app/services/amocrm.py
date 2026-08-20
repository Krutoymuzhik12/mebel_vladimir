"""amoCRM: кто уже общался с компанией → status=existing.

ТОЛЬКО ЧТЕНИЕ. В этом модуле нет ни одного метода записи — httpx.get и всё.
amoCRM у клиента боевая, с живыми сделками; правило держим на уровне кода.

Зачем: у Wazzup нет API, чтобы узнать историю переписки (эндпоинтов /chats и
/messages не существует — 404). Но встроенный коннектор Wazzup сам пишет
каждого написавшего в amoCRM, причём кладёт ТОЧНЫЙ chatId в отдельное поле
под каждый канал. Проверено на живых данных: TelegramId_WZ = 7936875555 —
ровно то, что приходит нам в вебхуке.

Так мы узнаём, с кем переписка уже была, и бот не здоровается со старым
клиентом как с незнакомцем.

Источники мержатся: файл data/existing_baseline.txt (руками) + amoCRM API.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import ROOT, Settings

logger = logging.getLogger(__name__)

DEFAULT_BASELINE_FILE = ROOT / "data" / "existing_baseline.txt"

# Поля, куда коннектор Wazzup кладёт идентификатор чата. Имена сверены с
# живым аккаунтом: у каждого канала своё поле.
WAZZUP_FIELDS = frozenset(
    {
        "TelegramId_WZ",
        "TelegramUsername_WZ",
        "WhatsappLid_WZ",
        "WhatsappUsername_WZ",
        "Whatsgroup_WZ",
        "Avito_WZ",
        "VK_WZ",
        "Instagram_WZ",
        "MaxId_WZ",
        "MaxgroupId_WZ",
    }
)

PHONE_FIELD = "Телефон"
PAGE_LIMIT = 250
MAX_PAGES = 100  # 25 000 контактов — с запасом, но не бесконечно
REQUEST_TIMEOUT = 40.0


def normalize_phone(raw: str) -> str:
    """Телефон → вид, в котором Wazzup отдаёт chatId для WhatsApp.

    В amoCRM номера лежат как попало: «89069235000», «+7 906 923-50-00»,
    «9069235000». В вебхуке WhatsApp chatId — это 79XXXXXXXXX без плюса.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("8"):
        return "7" + digits[1:]
    if len(digits) == 10:
        return "7" + digits
    return digits if len(digits) == 11 else ""


def _from_file(path: Path) -> list[str]:
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = line.split("#", 1)[0].strip()
        if raw:
            out.append(raw)
    return out


def _keys_from_contact(contact: dict[str, Any]) -> list[str]:
    """Все идентификаторы чатов этого контакта."""
    out: list[str] = []
    for field in contact.get("custom_fields_values") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("field_name") or "")
        is_wz = name in WAZZUP_FIELDS
        is_phone = name == PHONE_FIELD
        if not (is_wz or is_phone):
            continue
        for value in field.get("values") or []:
            if not isinstance(value, dict):
                continue
            raw = str(value.get("value") or "").strip()
            if not raw:
                continue
            if is_phone:
                phone = normalize_phone(raw)
                if phone:
                    out.append(phone)
            else:
                out.append(raw)
    return out


async def _from_amocrm_api(settings: Settings) -> list[str]:
    base = (settings.amocrm_base_url or "").strip().rstrip("/")
    token = (settings.amocrm_token or "").strip()
    if not base or not token or token.startswith("REPLACE_"):
        return []

    ids: list[str] = []
    async with httpx.AsyncClient(
        base_url=base,
        timeout=REQUEST_TIMEOUT,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            # Единственный вид запроса в этом модуле — GET
            resp = await client.get(
                "/api/v4/contacts", params={"limit": PAGE_LIMIT, "page": page}
            )
            if resp.status_code == 204:  # страницы кончились
                break
            if resp.status_code == 401:
                logger.error("amoCRM: 401 — проверьте AMOCRM_TOKEN и AMOCRM_BASE_URL")
                break
            if resp.status_code >= 400:
                logger.error(
                    "amoCRM contacts http=%s %s", resp.status_code, resp.text[:200]
                )
                break
            try:
                data = resp.json()
            except ValueError:
                logger.error("amoCRM: ответ не JSON на странице %s", page)
                break
            contacts = ((data.get("_embedded") or {}).get("contacts")) or []
            if not contacts:
                break
            for contact in contacts:
                if isinstance(contact, dict):
                    ids.extend(_keys_from_contact(contact))
        else:
            logger.warning(
                "amoCRM: остановился на %s странице — контактов больше, чем ждали",
                MAX_PAGES,
            )
    return ids


async def baseline_existing_ids(settings: Settings) -> list[str]:
    """Идентификаторы чатов, которые бот не должен считать новыми."""
    path = Path(settings.amocrm_baseline_file or DEFAULT_BASELINE_FILE)
    if not path.is_absolute():
        path = ROOT / path

    from_file = _from_file(path)
    from_api: list[str] = []
    try:
        from_api = await _from_amocrm_api(settings)
    except Exception:
        logger.exception("amoCRM baseline: API недоступен, берём только файл")

    if from_api:
        logger.info("amoCRM API: получено %s ключей", len(from_api))
    elif not (settings.amocrm_token or "").strip():
        logger.info(
            "amoCRM: токен не задан — baseline только из файла (%s шт.)",
            len(from_file),
        )

    # Тестовые чаты в «старые» не уводим ни при каких условиях
    excluded = settings.baseline_exclude_set

    seen: set[str] = set()
    out: list[str] = []
    skipped = 0
    for item in from_file + from_api:
        key = item.strip()
        if not key or key in seen:
            continue
        if key.lower() in excluded:
            skipped += 1
            continue
        seen.add(key)
        out.append(key)
    if skipped:
        logger.info("baseline: %s чатов исключены явно (тестовые)", skipped)
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
