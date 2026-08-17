"""Скачивание вложений клиента (голос, фото) по ссылке из Wazzup.

Ссылка приходит из вебхука, то есть снаружи. Поэтому качаем с потолком по
размеру и таймаутом: без них одна кривая ссылка кладёт процесс по памяти.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DOWNLOAD_TIMEOUT = 30.0

_SUFFIX_BY_KIND = {"voice": ".ogg", "image": ".jpg"}


def suffix_for(url: str, kind: str) -> str:
    """Расширение файла: по ссылке, иначе по типу сообщения."""
    path = urlparse(url or "").path
    dot = path.rfind(".")
    if 0 < dot and len(path) - dot <= 6:
        return path[dot:].lower()
    return _SUFFIX_BY_KIND.get(kind, ".bin")


async def download(url: str, *, max_bytes: int | None = None) -> bytes | None:
    """Содержимое вложения или None, если скачать не вышло."""
    if not url:
        return None
    limit = max_bytes or settings.media_max_bytes
    try:
        async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared and int(declared) > limit:
                    logger.warning(
                        "вложение слишком большое: %s байт при лимите %s",
                        declared,
                        limit,
                    )
                    return None
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > limit:
                        logger.warning("вложение переросло лимит %s байт", limit)
                        return None
                    chunks.append(chunk)
        return b"".join(chunks)
    except (httpx.HTTPError, ValueError):
        logger.exception("не удалось скачать вложение")
        return None
