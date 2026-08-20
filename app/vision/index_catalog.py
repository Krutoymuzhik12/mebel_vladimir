"""catalog/index.json → Qdrant.

Индексируем по новому каталогу: фото лежат не на диске, а на VK CDN, поэтому
качаем их один раз в data/catalog_photos/ и переиспользуем при пересборке.

В payload кладём тип, цвет и признаки — по ним поиск фильтрует кандидатов до
сравнения векторов. Без этого фото белой столешницы уверенно матчится с белым
диваном: по пикселям они и правда похожи, а клиенту это выглядит издевательством.

    python -m app.vision.index_catalog --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import httpx
from PIL import Image
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.vision.config import (
    CUT_BG_ON_INDEX,
    EMBED_DIM,
    QDRANT_COLLECTION,
    make_qdrant_client,
)
from app.catalog import photos
from app.vision.embedder import embed

ROOT = Path(__file__).resolve().parents[2]
INDEX_JSON = ROOT / "catalog" / "index.json"
PHOTO_DIR = ROOT / "data" / "catalog_photos"
DOWNLOAD_TIMEOUT = 30.0


def cached_photo(url: str) -> Path | None:
    """Файл с диска, при необходимости докачав.

    Тот же кэш, что использует отдача фото клиенту (app/catalog/photos.py):
    скачиваем каждую картинку один раз на оба применения.
    """
    return photos.fetch(url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Индексация каталога в Qdrant")
    parser.add_argument("--force", action="store_true",
                        help="пересоздать коллекцию")
    parser.add_argument("--limit", type=int, default=0,
                        help="сколько позиций взять (для пробного прогона)")
    args = parser.parse_args()

    if not INDEX_JSON.is_file():
        return int(bool(print(f"Нет {INDEX_JSON} — сначала python -m scripts.build_catalog")))

    items = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    if args.limit:
        items = items[: args.limit]

    qc = make_qdrant_client()
    if qc.collection_exists(QDRANT_COLLECTION):
        if not args.force:
            print(f"Коллекция '{QDRANT_COLLECTION}' уже есть, нужен --force")
            return 1
        qc.delete_collection(QDRANT_COLLECTION)
    qc.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )

    points: list[PointStruct] = []
    point_id = 0
    skipped = 0

    for n, item in enumerate(items, start=1):
        payload_base = {
            "article": item.get("article", ""),
            "name": item.get("name", ""),
            # По этим полям поиск отсекает заведомо не ту мебель
            "type": item.get("type", ""),
            "colors": item.get("colors") or [],
            "features": item.get("features") or [],
            "price": item.get("price") or 0,
            "price_text": item.get("price_text", ""),
        }
        for url in (item.get("photos") or [])[:3]:
            path = cached_photo(url)
            if path is None:
                skipped += 1
                continue
            try:
                vec = embed(Image.open(path), cut_bg=CUT_BG_ON_INDEX)
            except Exception as exc:
                print(f"  [skip] {item.get('article')}: {exc}", flush=True)
                skipped += 1
                continue
            points.append(PointStruct(
                id=point_id,
                vector=vec.tolist(),
                payload={**payload_base, "photo_url": url},
            ))
            point_id += 1

        if n % 25 == 0 or n == len(items):
            print(f"[{n}/{len(items)}] векторов={point_id} пропущено={skipped}",
                  flush=True)
        if len(points) >= 64:
            qc.upsert(collection_name=QDRANT_COLLECTION, points=points)
            points = []

    if points:
        qc.upsert(collection_name=QDRANT_COLLECTION, points=points)

    print(f"Готово: {point_id} векторов в '{QDRANT_COLLECTION}', пропущено {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
