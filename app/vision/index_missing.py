"""Доиндексация только тех фото, которых ещё нет в Qdrant (по photo_path)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image
from qdrant_client.models import PointStruct

from app.vision.config import (
    CATALOG_PATH,
    CUT_BG_ON_INDEX,
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
    qc = make_qdrant_client()
    if not qc.collection_exists(QDRANT_COLLECTION):
        sys.exit(f"нет коллекции {QDRANT_COLLECTION} — сначала полный index_catalog")

    # уже проиндексированные пути + max id
    indexed: set[str] = set()
    next_id = 0
    offset = None
    while True:
        records, offset = qc.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=256,
            offset=offset,
            with_payload=["photo_path"],
            with_vectors=False,
        )
        for r in records:
            if isinstance(r.id, int) and r.id >= next_id:
                next_id = r.id + 1
            path = (r.payload or {}).get("photo_path")
            if path:
                indexed.add(str(path).replace("\\", "/"))
        if offset is None:
            break

    print(f"уже в индексе: {len(indexed)} next_id={next_id}", flush=True)

    points: list[PointStruct] = []
    added = 0
    for art_dir in sorted(p for p in CATALOG_PATH.iterdir() if p.is_dir()):
        meta = _load_meta(art_dir)
        payload_base = {
            "article": art_dir.name,
            "colors": list(meta.get("colors") or []),
            "type": meta.get("type") or "",
            "name": meta.get("name") or art_dir.name,
            "price": meta.get("price") or "",
            "category": meta.get("category") or "",
        }
        for photo in sorted(art_dir.iterdir()):
            if photo.suffix.lower() not in IMAGE_EXTS or photo.stat().st_size < 4_000:
                continue
            rel = str(photo.relative_to(CATALOG_PATH.parent)).replace("\\", "/")
            if rel in indexed:
                continue
            try:
                vec = embed(Image.open(photo), cut_bg=CUT_BG_ON_INDEX)
            except Exception as exc:
                print(f"  skip {photo}: {exc}", flush=True)
                continue
            points.append(
                PointStruct(
                    id=next_id,
                    vector=vec.tolist(),
                    payload={**payload_base, "photo_path": rel},
                )
            )
            next_id += 1
            added += 1
            print(f"+ {rel}", flush=True)

    if points:
        qc.upsert(collection_name=QDRANT_COLLECTION, points=points)
    info2 = qc.get_collection(QDRANT_COLLECTION)
    print(f"добавлено {added}, всего points={info2.points_count}", flush=True)


if __name__ == "__main__":
    main()
