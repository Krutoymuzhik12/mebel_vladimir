"""Конфиг Vision: DINOv2 + локальный Qdrant. Пути якорим к ROOT проекта."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "vasha_mebel")
QDRANT_MODE = os.getenv("QDRANT_MODE", "local").lower()


def _resolve(raw: str, default_rel: str) -> Path:
    path = Path(raw or default_rel)
    return path if path.is_absolute() else ROOT / path


QDRANT_PATH = str(_resolve(os.getenv("QDRANT_PATH", ""), "data/qdrant_local"))
CATALOG_PATH = _resolve(os.getenv("CATALOG_PATH", ""), "catalog")

SIMILARITY_THRESHOLD = float(os.getenv("SIMILARITY_THRESHOLD", "0.42"))
CUT_BG = os.getenv("CUT_BG", "true").lower() in ("1", "true", "yes")
CUT_BG_ON_INDEX = os.getenv("CUT_BG_ON_INDEX", "false").lower() in ("1", "true", "yes")

EMBED_DIM = 384
TOP_K = int(os.getenv("VISION_TOP_K", "5"))


def make_qdrant_client():
    from qdrant_client import QdrantClient

    if QDRANT_MODE == "local":
        Path(QDRANT_PATH).mkdir(parents=True, exist_ok=True)
        return QdrantClient(path=QDRANT_PATH)
    return QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
