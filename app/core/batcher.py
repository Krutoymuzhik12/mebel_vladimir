"""Склейка пачки входящих — клиент часто дробит мысль на несколько сообщений."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.config import Settings
from app.transports.base import IncomingMessage

logger = logging.getLogger(__name__)

FlushHandler = Callable[[str, list[IncomingMessage]], Awaitable[None]]


@dataclass
class _Bucket:
    messages: list[IncomingMessage] = field(default_factory=list)
    first_at: float = 0.0
    last_at: float = 0.0
    task: asyncio.Task | None = None


class MessageBatcher:
    def __init__(self, settings: Settings, on_flush: FlushHandler) -> None:
        self.settings = settings
        self.on_flush = on_flush
        self._buckets: dict[str, _Bucket] = defaultdict(_Bucket)
        self._lock = asyncio.Lock()

    async def add(self, msg: IncomingMessage) -> None:
        async with self._lock:
            bucket = self._buckets[msg.chat_id]
            now = time.monotonic()
            if not bucket.messages:
                bucket.first_at = now
            bucket.messages.append(msg)
            bucket.last_at = now
            if bucket.task and not bucket.task.done():
                bucket.task.cancel()
            wait = self._next_wait(bucket)
            bucket.task = asyncio.create_task(self._wait_and_flush(msg.chat_id, wait))

    def _next_wait(self, bucket: _Bucket) -> float:
        if self.settings.fast_mode:
            return 0.3
        quiet = self.settings.message_batch_wait_sec
        max_wait = self.settings.message_batch_max_wait_sec
        elapsed = time.monotonic() - bucket.first_at
        remaining_cap = max(0.0, max_wait - elapsed)
        return min(quiet, remaining_cap) if max_wait > 0 else quiet

    async def _wait_and_flush(self, chat_id: str, wait: float) -> None:
        messages: list[IncomingMessage] | None = None
        try:
            await asyncio.sleep(wait)
            settle = (
                0.0 if self.settings.fast_mode else self.settings.message_batch_settle_sec
            )
            if settle:
                await asyncio.sleep(settle)
            async with self._lock:
                bucket = self._buckets.pop(chat_id, None)
                if bucket and bucket.messages:
                    messages = list(bucket.messages)
            if not messages:
                return
            logger.info("batch flush chat=%s n=%s", chat_id, len(messages))
            await self.on_flush(chat_id, messages)
        except asyncio.CancelledError:
            # если уже вынули сообщения — вернём в bucket
            if messages:
                async with self._lock:
                    bucket = self._buckets[chat_id]
                    bucket.messages = messages + bucket.messages
                    bucket.last_at = time.monotonic()
            return
