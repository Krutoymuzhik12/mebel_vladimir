"""Клиент Vision API + тонкая обёртка поиска по локальному каталогу."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class VisionClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or settings.vision_api_url).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(settings.vision_enabled)

    async def search_file(
        self,
        path: Path,
        *,
        top_k: int = 5,
        colors: str | None = None,
    ) -> dict[str, Any]:
        return await self.search_bytes(
            path.read_bytes(), filename=path.name, top_k=top_k, colors=colors
        )

    async def search_bytes(
        self,
        data: bytes,
        *,
        filename: str = "photo.jpg",
        top_k: int = 5,
        colors: str | None = None,
    ) -> dict[str, Any]:
        """Фото приходит из вебхука в памяти — на диск его класть незачем."""
        if not self.enabled:
            return {"found": False, "matches": [], "error": "vision disabled"}
        form: dict[str, Any] = {"top_k": str(top_k)}
        if colors:
            form["colors"] = colors
        async with httpx.AsyncClient(timeout=60.0) as client:
            files = {"file": (filename, data, "image/jpeg")}
            r = await client.post(f"{self.base_url}/search", data=form, files=files)
            r.raise_for_status()
            return r.json()
