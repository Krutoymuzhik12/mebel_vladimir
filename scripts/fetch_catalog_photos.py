"""Забрать все фотографии каталога на сервер.

В catalog/index.json лежат ссылки на VK. Этот скрипт скачивает их в
data/catalog_photos/, после чего бот отдаёт фото клиенту с диска, а Qdrant
получает готовые файлы для индексации — качать второй раз не придётся.

Повторный запуск докачивает только недостающее, так что прерванную загрузку
можно спокойно продолжить.

    python -m scripts.fetch_catalog_photos            # все
    python -m scripts.fetch_catalog_photos --limit 50 # пробный прогон

Ничего никуда не отправляет: только GET на CDN и запись на диск.
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from app.catalog import photos
from app.catalog.search import shared

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Скачать фото каталога")
    parser.add_argument("--limit", type=int, default=0, help="сколько позиций взять")
    parser.add_argument(
        "--per-item", type=int, default=3, help="сколько фото с позиции (по умолчанию 3)"
    )
    args = parser.parse_args()

    catalog = shared()
    if not catalog.loaded:
        print("Каталог пуст. Сначала: python -m scripts.build_catalog <файл.xlsx>")
        return 1

    items = catalog.items[: args.limit] if args.limit else catalog.items
    urls: list[str] = []
    for item in items:
        urls.extend((item.get("photos") or [])[: max(1, args.per_item)])

    total = len(urls)
    print(f"Позиций: {len(items)}, фотографий к загрузке: {total}")
    print(f"Каталог файлов: {photos.PHOTO_DIR}")

    done = skipped = failed = 0
    started = time.monotonic()
    with httpx.Client(timeout=photos.DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
        for n, url in enumerate(urls, start=1):
            if photos.is_cached(url):
                skipped += 1
            elif photos.fetch(url, client=client) is not None:
                done += 1
            else:
                failed += 1

            if n % 50 == 0 or n == total:
                elapsed = time.monotonic() - started
                speed = n / elapsed if elapsed else 0
                left = (total - n) / speed if speed else 0
                print(
                    f"[{n}/{total}] скачано={done} уже было={skipped} "
                    f"ошибок={failed} | осталось ~{left / 60:.1f} мин",
                    flush=True,
                )

    size_mb = sum(f.stat().st_size for f in photos.PHOTO_DIR.glob("*.jpg")) / 1024 / 1024
    print(
        f"\nГотово: скачано {done}, уже было {skipped}, не вышло {failed}. "
        f"На диске {size_mb:.0f} МБ"
    )
    if failed:
        print("Неудачные можно добрать повторным запуском — он не качает заново.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
