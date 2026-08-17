"""Список каналов Wazzup: channelId, тип, состояние.

Нужен, чтобы взять UUID канала для TEST_CHANNEL_IDS.

    python -m scripts.wazzup_channels
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.transports.wazzup import WazzupTransport


async def main() -> int:
    transport = WazzupTransport(settings)
    if not transport.configured:
        print("WAZZUP_API_KEY не задан в .env")
        return 1
    try:
        channels = await transport.list_channels()
    except Exception as exc:
        print(f"Ошибка: {exc}")
        return 1
    finally:
        await transport.aclose()

    if not channels:
        print("Каналов нет (или ключ без доступа).")
        return 1

    print(f"Каналов: {len(channels)}\n")
    for ch in channels:
        print(f"  channelId : {ch.get('channelId')}")
        print(f"  тип       : {ch.get('transport')}")
        print(f"  название  : {ch.get('name') or ch.get('plainId') or '—'}")
        print(f"  состояние : {ch.get('state')}")
        print("  " + "-" * 46)

    print("\nДля тестового режима скопируйте нужный channelId в .env:")
    print("  TEST_MODE=1")
    print("  TEST_CHANNEL_IDS=<сюда channelId>")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
