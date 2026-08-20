"""Отдача фото каталога под своим коротким адресом.

Каталог хранит прямые ссылки VK вида
    https://sun9-34.userapi.com/s/v1/ig2/<хеш>.jpg?quality=95&as=32x23,48x34,…&u=…&cs=1600x0
Это 300+ символов, из них две трети — подписанный хвост параметров. Wazzup
такую ссылку не принимает: отвечает 400 INVALID_MESSAGE_DATA, и фото до
клиента не доходит (поймано на живом тесте).

Обрезать хвост нельзя — без подписи VK отдаёт 403. Поэтому отдаём картинку
сами: короткий адрес на нашем домене, оканчивающийся на .jpg, а байты
подтягиваем из VK на лету.

Подставить чужой URL через этот эндпоинт нельзя: наружу уходит только то,
что уже лежит в нашем catalog/index.json — снаружи принимаются лишь артикул
и номер фото.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.catalog import photos as catalog_photos
from app.catalog import search as catalog_search

logger = logging.getLogger(__name__)

router = APIRouter()

FETCH_TIMEOUT = 20.0
# Больше пары мегабайт фото каталога не бывает; ограничение против сюрпризов
MAX_BYTES = 8 * 1024 * 1024


@router.get("/media/{article}/{index}.jpg")
async def catalog_photo(article: str, index: int) -> Response:
    catalog = catalog_search.shared()
    item = next(
        (i for i in catalog.items if str(i.get("article")) == article), None
    )
    if item is None:
        raise HTTPException(status_code=404, detail="unknown article")

    photos = item.get("photos") or []
    if not 0 <= index < len(photos):
        raise HTTPException(status_code=404, detail="no such photo")

    url = str(photos[index])

    # Скачанное лежит на диске — отдаём оттуда: быстрее и не зависит от того,
    # жива ли подписанная ссылка VK. В VK ходим только если файла ещё нет.
    cached = catalog_photos.local_path(url)
    if catalog_photos.is_cached(url):
        return Response(
            content=cached.read_bytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=604800"},
        )

    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as c:
            resp = await c.get(url)
    except httpx.HTTPError:
        logger.exception("фото каталога недоступно: %s", article)
        raise HTTPException(status_code=502, detail="upstream unavailable") from None

    if resp.status_code != 200:
        logger.warning(
            "фото каталога %s[%s]: источник ответил %s", article, index, resp.status_code
        )
        raise HTTPException(status_code=502, detail="upstream error")

    data = resp.content
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=502, detail="image too large")

    return Response(
        content=data,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=604800"},
    )
