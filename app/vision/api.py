"""Vision Search API: фото клиента → похожие позиции каталога."""

from __future__ import annotations

import io
import logging
from collections import defaultdict
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from PIL import Image
from qdrant_client.models import FieldCondition, Filter, MatchAny

from app.vision.config import (
    QDRANT_COLLECTION,
    QDRANT_PATH,
    SIMILARITY_THRESHOLD,
    TOP_K,
    make_qdrant_client,
)
from app.vision.embedder import embed

logger = logging.getLogger(__name__)

app = FastAPI(title="Vasha Mebel Vision Search")

# Хранилище открываем при старте приложения, а не при импорте модуля.
# Qdrant в локальном режиме пускает к папке ровно один процесс, и раньше
# конфликт блокировки убивал процесс прямо на импорте — uvicorn не успевал
# даже подняться, а в логе был голый traceback вместо внятной причины.
_qc = None


def qdrant():
    """Клиент Qdrant. Открывается лениво и переживает временную занятость."""
    global _qc
    if _qc is None:
        _qc = make_qdrant_client()
    return _qc


@app.on_event("startup")
def _open_storage() -> None:
    try:
        qdrant()
        logger.info("Qdrant открыт: %s", QDRANT_COLLECTION)
    except RuntimeError as exc:
        # Понятное сообщение вместо трейсбека: почти всегда это оставшийся
        # процесс vision, который ещё держит папку индекса.
        logger.error(
            "Не открыть хранилище Qdrant: %s. Обычно это другой экземпляр "
            "vision — проверьте: fuser -v %s/.lock",
            exc,
            QDRANT_PATH,
        )
        raise


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
    types: Optional[str] = Query(None),
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
    type_list = _parse_colors(types)
    # Тип мебели отсекает кандидатов ДО сравнения векторов. Фото белой
    # столешницы по пикселям похоже на белый диван, и без этого фильтра
    # выдача выглядит случайной.
    conditions = []
    if type_list:
        conditions.append(FieldCondition(key="type", match=MatchAny(any=type_list)))
    if color_list:
        conditions.append(FieldCondition(key="colors", match=MatchAny(any=color_list)))
    query_filter = Filter(must=conditions) if conditions else None

    try:
        hits = qdrant().query_points(
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
            "photos": [best_payload[a].get("photo_url")]
            if best_payload[a].get("photo_url") else [],
            "features": best_payload[a].get("features") or [],
            "colors": best_payload[a].get("colors") or [],
            "name": best_payload[a].get("name") or a,
            "type": best_payload[a].get("type") or "",
            "price": best_payload[a].get("price") or "",
        }
        for a, s in sorted(best_score.items(), key=lambda x: -x[1])
        if s >= threshold
    ][: max(1, min(top_k, 10))]

    return {"found": bool(results), "matches": results,
            "color_filter": color_list, "type_filter": type_list}


@app.get("/health")
async def health():
    try:
        info = qdrant().get_collection(QDRANT_COLLECTION)
        points = info.points_count
        qdrant_ok = True
    except Exception:
        points, qdrant_ok = None, False
    return {"ok": True, "qdrant": qdrant_ok, "indexed_points": points}
