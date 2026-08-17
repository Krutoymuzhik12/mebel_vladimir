"""Кто наш бот в MAX и в каких он чатах.

    python -m scripts.max_chats            — профиль бота и список чатов
    python -m scripts.max_chats --updates  — плюс сырые события (нажатия кнопок)

Второй режим нужен один раз: сверить формат события с живым MAX, прежде чем
полагаться на разбор полей. Читает и печатает, ничего не отправляет.
"""

from __future__ import annotations

import asyncio
import json
import sys

import httpx

from app.config import settings
from app.notify.max import API_BASE


async def main(argv: list[str]) -> int:
    token = settings.max_bot_token.strip()
    if not token:
        print("MAX_BOT_TOKEN не задан в .env")
        return 1

    async with httpx.AsyncClient(
        base_url=API_BASE, timeout=60.0, headers={"Authorization": token}
    ) as client:
        me = await client.get("/me")
        if me.status_code >= 400:
            print(f"Токен не принят ({me.status_code}): {me.text[:200]}")
            return 1
        bot = me.json()
        print(f"Бот: {bot.get('name')} (@{bot.get('username')}), id={bot.get('user_id')}")

        chats = await client.get("/chats", params={"count": 50})
        items = (chats.json() or {}).get("chats") or []
        print(f"\nЧатов: {len(items)}")
        if not items:
            print(
                "  Бот пока никуда не добавлен.\n"
                "  Добавьте его в рабочую группу MAX (или напишите ему в личку),\n"
                "  отправьте туда любое сообщение и запустите эту команду снова."
            )
        for c in items:
            print("-" * 46)
            print(f"  chat_id : {c.get('chat_id')}")
            print(f"  тип     : {c.get('type')}")
            print(f"  название: {c.get('title') or '(без названия)'}")
            print(f"  статус  : {c.get('status')}")
        if items:
            print("-" * 46)
            print("\nНужный chat_id скопируйте в .env:")
            print("  MAX_ENABLED=1")
            print("  MAX_GROUP_ID=<сюда chat_id>")

        if "--updates" in argv:
            print("\nЖду события 60 секунд — нажмите кнопку или напишите боту…")
            resp = await client.get(
                "/updates", params={"timeout": 60, "limit": 20}, timeout=120.0
            )
            data = resp.json() or {}
            updates = data.get("updates") or []
            print(f"Событий: {len(updates)} (marker={data.get('marker')})")
            for u in updates:
                print("-" * 46)
                print(json.dumps(u, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
