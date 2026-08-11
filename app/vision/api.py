"""Vision Search API: фото клиента → похожие позиции каталога."""

from __future__ import annotations

import io
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from PIL import Image
from qdrant_client.models import FieldCondition, Filter, MatchAny

from app.vision.config import (
    QDRANT_COLLECTION,
    SIMILARITY_THRESHOLD,
    TOP_K,
    make_qdrant_client,
)
from app.vision.embedder import embed

app = FastAPI(title="Vasha Mebel Vision Search")
qc = make_qdrant_client()


def _parse_colors(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [c.strip().lower() for c in raw.split(",") if c.strip()]


@app.post("/search")
async def search(
    file: UploadFile = File(...),
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = TOP_K,
    colors: Optional[str] = Query(None),
):
    raw = await file.read()
    if len(raw) > 8_000_000:
        raise HTTPException(status_code=413, detail="image too large (max 8MB)")
    try:
        img = Image.open(io.BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"bad image: {exc}") from exc

    vec = embed(img, cut_bg=True)
    color_list = _parse_colors(colors)
    query_filter = None
    if color_list:
        query_filter = Filter(
            must=[FieldCondition(key="colors", match=MatchAny(any=color_list))]
        )

    try:
        hits = qc.query_points(
            collection_name=QDRANT_COLLECTION,
            query=vec.tolist(),
            query_filter=query_filter,
            limit=40 if color_list else 24,
        ).points
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Qdrant: {exc}") from exc

    best_score: dict[str, float] = defaultdict(float)
    best_payload: dict[str, dict] = {}
    for h in hits:
        art = (h.payload or {}).get("article")
        if not art:
            continue
        if h.score > best_score[art]:
            best_score[art] = h.score
            best_payload[art] = h.payload or {}

    results = [
        {
            "article": a,
            "similarity": round(s, 3),
            "photo_path": best_payload[a].get("photo_path", ""),
            "colors": best_payload[a].get("colors") or [],
            "name": best_payload[a].get("name") or a,
            "type": best_payload[a].get("type") or "",
            "price": best_payload[a].get("price") or "",
        }
        for a, s in sorted(best_score.items(), key=lambda x: -x[1])
        if s >= threshold
    ][: max(1, min(top_k, 10))]

    return {"found": bool(results), "matches": results, "color_filter": color_list}


@app.get("/health")
async def health():
    try:
        info = qc.get_collection(QDRANT_COLLECTION)
        points = info.points_count
        qdrant_ok = True
    except Exception:
        points, qdrant_ok = None, False
    return {"ok": True, "qdrant": qdrant_ok, "indexed_points": points}
