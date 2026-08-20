"""Локальные копии фотографий каталога.

В catalog/index.json лежат ссылки на VK CDN. Полагаться на них в рантайме
плохо по двум причинам: ссылка подписана и когда-нибудь протухнет, а каждая
отдача клиенту превращается в поход на чужой сервер.

Поэтому фото один раз скачиваются сюда, а дальше используются с диска — и
отдачей клиенту, и индексацией в Qdrant. Имя файла считается от адреса,
так что повторный запуск ничего не перекачивает.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import httpx

from app.config import ROOT

logger = logging.getLogger(__name__)

PHOTO_DIR = ROOT / "data" / "catalog_photos"
DOWNLOAD_TIMEOUT = 30.0
# Меньше двух килобайт — это не фотография, а заглушка или ошибка
MIN_BYTES = 2000


def local_path(url: str) -> Path:
    """Куда ляжет этот адрес. Файла может ещё не быть."""
    name = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:20] + ".jpg"
    return PHOTO_DIR / name


def is_cached(url: str) -> bool:
    path = local_path(url)
    return path.exists() and path.stat().st_size >= MIN_BYTES


def fetch(url: str, *, client: httpx.Client | None = None) -> Path | None:
    """Скачать, если ещё нет. Возвращает путь или None, если не вышло."""
    path = local_path(url)
    if is_cached(url):
        return path

    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    own = client is None
    c = client or httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
    try:
        resp = c.get(url)
        if resp.status_code != 200:
            logger.warning("фото %s: источник ответил %s", url[:60], resp.status_code)
            return None
        data = resp.content
        if len(data) < MIN_BYTES:
            logger.warning("фото %s: слишком маленькое (%s байт)", url[:60], len(data))
            return None
        # Пишем через временный файл: оборванная закачка не должна оставить
        # битый файл, который потом сочтут скачанным
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path
    except (httpx.HTTPError, OSError):
        logger.exception("фото не скачалось: %s", url[:60])
        return None
    finally:
        if own:
            c.close()
