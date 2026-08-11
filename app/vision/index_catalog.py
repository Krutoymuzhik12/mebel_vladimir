"""Индексация catalog/ → Qdrant (DINOv2 + rembg на индексе для максимального качества)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.vision.config import (
    CATALOG_PATH,
    CUT_BG_ON_INDEX,
    EMBED_DIM,
    QDRANT_COLLECTION,
    make_qdrant_client,
)
from app.vision.embedder import embed

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _load_meta(art_dir: Path) -> dict:
    p = art_dir / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Index catalog → Qdrant")
    parser.add_argument(
        "--force",
        action="store_true",
        help="удалить и пересоздать коллекцию (иначе отказ если уже есть)",
    )
    args = parser.parse_args()

    if not CATALOG_PATH.is_dir():
        sys.exit(f"Каталог не найден: {CATALOG_PATH}")

    qc = make_qdrant_client()
    if qc.collection_exists(QDRANT_COLLECTION):
        if not args.force:
            sys.exit(
                f"Коллекция '{QDRANT_COLLECTION}' уже есть. "
                "Передайте --force для полной пересборки или используйте index_missing."
            )
        qc.delete_collection(QDRANT_COLLECTION)
    qc.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )

    articles = [d for d in sorted(CATALOG_PATH.iterdir()) if d.is_dir()]
    if not articles:
        sys.exit(f"В {CATALOG_PATH} нет папок артикулов")

    points: list[PointStruct] = []
    point_id = 0
    skipped = 0

    for i, art_dir in enumerate(articles, start=1):
        meta = _load_meta(art_dir)
        payload_base = {
            "article": art_dir.name,
            "colors": list(meta.get("colors") or []),
            "type": meta.get("type") or "",
            "name": meta.get("name") or art_dir.name,
            "price": meta.get("price") or "",
            "category": meta.get("category") or "",
        }
        photos = [
            p
            for p in sorted(art_dir.iterdir())
            if p.suffix.lower() in IMAGE_EXTS and p.stat().st_size >= 4_000
        ]
        for photo in photos:
            try:
                vec = embed(Image.open(photo), cut_bg=CUT_BG_ON_INDEX)
            except Exception as exc:
                print(f"  [skip] {photo}: {exc}", flush=True)
                skipped += 1
                continue
            points.append(
                PointStruct(
                    id=point_id,
                    vector=vec.tolist(),
                    payload={
                        **payload_base,
                        "photo_path": str(photo.relative_to(CATALOG_PATH.parent)).replace(
                            "\\", "/"
                        ),
                    },
                )
            )
            point_id += 1

        if i % 25 == 0 or i == len(articles):
            print(
                f"[{i}/{len(articles)}] vectors={point_id} last={art_dir.name}",
                flush=True,
            )

        if len(points) >= 64:
            qc.upsert(collection_name=QDRANT_COLLECTION, points=points)
            points = []

    if points:
        qc.upsert(collection_name=QDRANT_COLLECTION, points=points)

    print(
        f"Готово: {point_id} векторов в '{QDRANT_COLLECTION}', skipped={skipped}",
        flush=True,
    )


if __name__ == "__main__":
    main()
