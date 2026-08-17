"""Регистрация webhook-URL в Wazzup.

Wazzup сразу проверяет URL запросом — сервис должен быть уже поднят и доступен
из интернета, иначе подписка не пройдёт.

    python -m scripts.wazzup_webhook          # показать текущий
    python -m scripts.wazzup_webhook --set    # записать PUBLIC_WEBHOOK_URL
    python -m scripts.wazzup_webhook --set https://example.com/webhooks/wazzup/secret
"""

from __future__ import annotations

import asyncio
import sys

from app.config import settings
from app.transports.wazzup import WazzupTransport


def _target_url(argv: list[str]) -> str:
    for i, arg in enumerate(argv):
        if arg == "--set" and i + 1 < len(argv):
            return argv[i + 1]
    base = (settings.public_webhook_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "/webhooks/wazzup" in base:
        return base
    secret = (settings.wazzup_webhook_secret or "").strip()
    return f"{base}/webhooks/wazzup/{secret}" if secret else f"{base}/webhooks/wazzup"


async def main(argv: list[str]) -> int:
    transport = WazzupTransport(settings)
    if not transport.configured:
        print("WAZZUP_API_KEY не задан в .env")
        return 1

    try:
        if "--set" not in argv:
            current = await transport.get_webhook()
            print("Текущая подписка:")
            print(f"  {current}")
            print("\nЗаписать новую: python -m scripts.wazzup_webhook --set")
            return 0

        url = _target_url(argv)
        if not url:
            print(
                "Не задан URL. Заполните PUBLIC_WEBHOOK_URL в .env "
                "или передайте адрес: --set https://.../webhooks/wazzup/<secret>"
            )
            return 1
        if not url.startswith("https://"):
            print(f"Wazzup принимает только https. Получено: {url}")
            return 1

        print(f"Регистрирую вебхук: {url}")
        result = await transport.set_webhook(url)
        print(f"Готово: {result}")
        return 0
    except Exception as exc:
        print(f"Ошибка: {exc}")
        print(
            "\nЧастые причины:\n"
            "  - сервис недоступен снаружи (Wazzup проверяет URL сразу)\n"
            "  - нет https / невалидный сертификат\n"
            "  - сервис не отвечает 200 на этот путь"
        )
        return 1
    finally:
        await transport.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
