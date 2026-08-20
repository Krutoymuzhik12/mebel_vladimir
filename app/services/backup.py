"""Периодический бэкап SQLite bot.db."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def backup_db(db_path: Path, backup_dir: Path, *, keep: int = 14) -> Path | None:
    """Скопировать БД в backup_dir. Старые файлы сверх keep — удалить."""
    if not db_path.is_file():
        logger.warning("бэкап: нет файла %s", db_path)
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dest = backup_dir / f"bot_{stamp}.db"
    try:
        shutil.copy2(db_path, dest)
    except OSError:
        logger.exception("бэкап не удался")
        return None
    logger.info("бэкап БД → %s", dest)
    _prune(backup_dir, keep=keep)
    return dest


def _prune(backup_dir: Path, *, keep: int) -> None:
    files = sorted(backup_dir.glob("bot_*.db"), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep] if keep > 0 else files:
        try:
            old.unlink()
            logger.info("бэкап удалён (лимит): %s", old.name)
        except OSError:
            logger.warning("не удалось удалить старый бэкап %s", old)
