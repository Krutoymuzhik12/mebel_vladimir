"""Разведка amoCRM: что там лежит для сопоставления старых чатов.

ТОЛЬКО ЧТЕНИЕ. В этом файле нет ни одного метода записи — httpx.get и всё.
amoCRM у клиента боевая, с живыми сделками, любая запись туда запрещена.

Задача: найти поле, в которое коннектор Wazzup кладёт идентификатор чата
(в интерфейсе видели «Avito_WZ» со значением вида u2i-1WESq62...). Если такие
поля есть под каждый канал — сможем разом пометить всех, с кем переписка уже
была, и бот не поздоровается с ними как с незнакомцами.

    python -m scripts.amocrm_probe

Читает AMOCRM_BASE_URL и AMOCRM_TOKEN из .env. Токен не печатает.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from app.config import settings

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TIMEOUT = 30.0
# Значения полей обрезаем: нам нужен формат, а не персональные данные клиентов
VALUE_CUT = 60


def _short(value: object) -> str:
    text = str(value)
    return text if len(text) <= VALUE_CUT else text[:VALUE_CUT] + "…"


async def _get(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    """Единственный способ сходить в amoCRM в этом скрипте."""
    try:
        resp = await client.get(path, params=params or None)
    except httpx.HTTPError as exc:
        print(f"  сеть не дала: {exc}")
        return None
    if resp.status_code == 204:
        print(f"  {path}: пусто (204)")
        return None
    if resp.status_code >= 400:
        print(f"  {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return None
    try:
        return resp.json()
    except ValueError:
        print(f"  {path}: ответ не JSON")
        return None


def _print_fields(title: str, data: dict | None) -> list[dict]:
    print(f"\n=== {title} ===")
    items = ((data or {}).get("_embedded") or {}).get("custom_fields") or []
    if not items:
        print("  (пусто)")
        return []
    wazzup_like = []
    for f in items:
        name = str(f.get("name") or "")
        code = str(f.get("code") or "")
        ftype = str(f.get("type") or "")
        marker = ""
        # Поля от коннектора Wazzup обычно помечены каналом в названии
        if any(k in (name + code).lower() for k in ("wz", "wazzup", "avito", "whats", "telegram", "vk", "insta")):
            marker = "  <<< похоже на канал Wazzup"
            wazzup_like.append(f)
        print(f"  id={f.get('id'):<12} {ftype:<14} {name!r} code={code!r}{marker}")
    return wazzup_like


def _print_entity(title: str, data: dict | None, key: str) -> None:
    print(f"\n=== {title} ===")
    items = ((data or {}).get("_embedded") or {}).get(key) or []
    if not items:
        print("  (пусто)")
        return
    for item in items:
        print(f"\n  --- id={item.get('id')} name={_short(item.get('name'))}")
        cfs = item.get("custom_fields_values") or []
        if not cfs:
            print("      custom_fields_values: пусто")
        for f in cfs:
            fname = f.get("field_name") or f.get("field_code") or f.get("field_id")
            values = [v.get("value") for v in (f.get("values") or []) if isinstance(v, dict)]
            shown = ", ".join(_short(v) for v in values) or "—"
            print(f"      {str(fname):<24} = {shown}")


async def main() -> int:
    base = (settings.amocrm_base_url or "").strip().rstrip("/")
    token = (settings.amocrm_token or "").strip()

    if not base or not token or token.startswith("REPLACE_"):
        print(
            "Не задан AMOCRM_BASE_URL / AMOCRM_TOKEN в .env.\n"
            "Пример:\n"
            "  AMOCRM_BASE_URL=https://поддомен.amocrm.ru\n"
            "  AMOCRM_TOKEN=<долгосрочный токен>"
        )
        return 1

    print(f"amoCRM: {base}  (режим: только чтение)")
    async with httpx.AsyncClient(
        base_url=base,
        timeout=TIMEOUT,
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        account = await _get(client, "/api/v4/account")
        if account is None:
            print("\nАвторизация не прошла — дальше идти смысла нет.")
            return 1
        print(f"Аккаунт: {account.get('name')} (id={account.get('id')})")

        _print_fields(
            "Поля КОНТАКТОВ", await _get(client, "/api/v4/contacts/custom_fields", limit=250)
        )
        _print_fields(
            "Поля СДЕЛОК", await _get(client, "/api/v4/leads/custom_fields", limit=250)
        )

        _print_entity(
            "Примеры контактов", await _get(client, "/api/v4/contacts", limit=5), "contacts"
        )
        _print_entity(
            "Примеры сделок", await _get(client, "/api/v4/leads", limit=5), "leads"
        )

        # Сколько всего контактов — оценим объём разового импорта
        page = await _get(client, "/api/v4/contacts", limit=1)
        total = (page or {}).get("_page_count")
        if total:
            print(f"\nСтраниц контактов по 1 шт: {total} (примерно столько контактов)")

    print("\nГотово. Ничего не изменено — выполнялись только GET-запросы.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
