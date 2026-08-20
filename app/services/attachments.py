"""Разбор вложений клиента (PDF/DOCX) → текст для модели.

Бесплатный стек:
- pypdf — текст из PDF (планы, КП, сметы)
- python-docx — текст из .docx (если установлен)

Стикеры и видео сознательно игнорируются на уровне orchestrator (не отвечаем).
Скан PDF без текстового слоя: говорим менеджер-боту попросить описание словами.
"""

from __future__ import annotations

import io
import logging
from urllib.parse import urlparse

from app.services import media

logger = logging.getLogger(__name__)

MAX_EXTRACT_CHARS = 4000

_HINTS = {
    "document": (
        "Клиент прислал документ. Если ниже есть извлечённый текст — опирайся "
        "только на него. Если текста нет — скажи, что файл не разобрать, и "
        "попроси описать размеры/материал словами или передать менеджеру."
    ),
    "geo": (
        "Клиент прислал геолокацию. Поблагодари и при необходимости уточни "
        "адрес текстом (город / улица) для замера."
    ),
    "unsupported": (
        "Клиент прислал вложение, которое система не умеет открыть. "
        "Скажи коротко, что не видишь файл, и попроси описать словами или "
        "прислать фото / текст."
    ),
}


def hint_for(kind: str) -> str:
    return _HINTS.get(kind, "")


def _name_from_url(url: str) -> str:
    path = urlparse(url or "").path
    return path.rsplit("/", 1)[-1].lower() if path else ""


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf не установлен — PDF не разбираем (pip install pypdf)")
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages[:20]:
            text = (page.extract_text() or "").strip()
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    except Exception:
        logger.exception("не удалось разобрать PDF")
        return ""


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        return ""
    try:
        doc = Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
    except Exception:
        logger.exception("не удалось разобрать DOCX")
        return ""


def extract_bytes(data: bytes, *, filename: str = "", content_type: str = "") -> str:
    """Текст из файла или пустая строка."""
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return _extract_pdf(data)[:MAX_EXTRACT_CHARS]
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return _extract_docx(data)[:MAX_EXTRACT_CHARS]
    # .doc / xlsx / zip — пока не трогаем
    return ""


async def extract_from_url(url: str, *, filename: str = "") -> str:
    raw = await media.download(url)
    if not raw:
        return ""
    name = filename or _name_from_url(url)
    return extract_bytes(raw, filename=name)
