"""Обёртка над Poe API (OpenAI-совместимый эндпоинт).

Роли:
  POE_MANAGER_BOT    = Vladimir_dialog   → реплика клиенту
  POE_CLASSIFIER_BOT = Vladimir_Intent   → JSON интент

SEND_SYSTEM_PROMPTS=false: промпты зашиты в ботах на Poe.
true: шлём prompts/*.txt системным сообщением (удобно для отладки).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from app.config import ROOT, settings

logger = logging.getLogger(__name__)

PROMPTS_DIR = ROOT / "prompts"
_prompt_cache: dict[str, str] = {}

_BANNED_OPENERS_RE = re.compile(
    r"^\s*(?:понял(?:\s+вас)?|принял|ясно|ясненько|хорошо|бывает|ну\s+да|"
    r"ничего\s+страшного|как\s+скажете)\b[\s,.!:;-]*",
    re.I,
)


def read_prompt(name: str) -> str:
    if not settings.send_system_prompts:
        return ""
    if name in _prompt_cache:
        return _prompt_cache[name]
    path = PROMPTS_DIR / name
    if not path.exists():
        logger.warning("Промпт %s не найден", path)
        return ""
    text = (
        path.read_text(encoding="utf-8")
        .replace("{COMPANY_NAME}", settings.company_name)
        .replace("{MANAGER_NAME}", settings.manager_name)
    )
    _prompt_cache[name] = text
    return text


def _system(prompt_file: str) -> list[dict[str, str]]:
    text = read_prompt(prompt_file)
    return [{"role": "system", "content": text}] if text else []


_GREETING_RE = re.compile(
    r"^\s*(?:здравствуйте|здравствуй|добрый\s+день|доброе\s+утро|"
    r"добрый\s+вечер|доброго\s+времени(?:\s+суток)?|приветствую|привет)"
    r"[\s,!.:;–—-]*",
    re.I,
)


def strip_greeting(text: str) -> str:
    """Срезать приветствие в начале реплики.

    Модель здоровается в каждом сообщении, даже посреди разговора, и иногда
    дважды подряд («Здравствуйте! добрый день!»). Запрет в промпте помогает
    не всегда, а выглядит это хуже всего остального: сразу видно робота,
    который не помнит, что уже говорил с человеком. Поэтому режем в коде,
    и только там, где приветствие точно лишнее — не в первом обращении.
    """
    clean = (text or "").strip()
    for _ in range(3):  # «Здравствуйте! Добрый день!» — два подряд
        stripped = _GREETING_RE.sub("", clean, count=1)
        if stripped == clean:
            break
        clean = stripped.lstrip()
    if not clean:
        return text
    return clean[0].upper() + clean[1:]


def strip_banned_openers(text: str) -> str:
    clean = _BANNED_OPENERS_RE.sub("", text or "", count=1)
    if clean and clean != text:
        clean = clean[0].upper() + clean[1:]
    return clean or text


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*{1,3}(\S(?:.*?\S)?)\*{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`{1,3}(.+?)`{1,3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text.replace("—", "-").replace("–", "-")


def parse_json(raw: str) -> dict[str, Any] | None:
    if not raw:
        return None
    txt = raw.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", txt, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                return None
    return None


class PoeClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        self.api_key = api_key if api_key is not None else settings.poe_api_key
        self.base_url = (base_url or settings.poe_base_url).rstrip("/")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        max_tokens: int = 900,
    ) -> str:
        if not self.enabled:
            raise RuntimeError("POE_API_KEY не задан в .env")
        if not model:
            raise RuntimeError("Не указано имя бота Poe в .env")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            if resp.status_code == 401:
                raise RuntimeError("POE: неверный API key (401)")
            if resp.status_code == 404:
                raise RuntimeError(f"POE: бот «{model}» не найден (404)")
            resp.raise_for_status()
            data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    async def manager_reply(
        self, history: list[dict[str, str]], context_block: str
    ) -> str:
        messages = _system("manager.txt")
        messages.extend({"role": m["role"], "content": m["content"]} for m in history)
        messages.append({"role": "user", "content": context_block})
        raw = await self.chat(settings.poe_manager_bot, messages, temperature=0.7)
        return strip_banned_openers(strip_markdown(raw).strip())

    async def classify(
        self, history: list[dict[str, str]], user_msg: str, known: dict[str, Any]
    ) -> dict[str, Any] | None:
        last_manager = next(
            (m["content"] for m in reversed(history) if m["role"] == "assistant"), ""
        )
        transcript = "\n".join(
            f"{'Клиент' if m['role'] == 'user' else 'Менеджер'}: {m['content']}"
            for m in history[-12:]
        )
        known_text = (
            "\n".join(f"{k}: {v}" for k, v in (known or {}).items() if v) or "пока пусто"
        )
        messages = _system("classifier.txt")
        messages.append(
            {
                "role": "user",
                "content": (
                    f"ЧТО УЖЕ ИЗВЕСТНО О КЛИЕНТЕ:\n{known_text}\n\n"
                    f"ИСТОРИЯ ДИАЛОГА:\n{transcript or '(пусто)'}\n\n"
                    f"ПОСЛЕДНЯЯ РЕПЛИКА МЕНЕДЖЕРА: {last_manager or '(ещё не было)'}\n\n"
                    f"СООБЩЕНИЕ КЛИЕНТА ДЛЯ КЛАССИФИКАЦИИ:\n{user_msg}\n\n"
                    "Верни строго JSON по заданному формату."
                ),
            }
        )
        raw = await self.chat(
            settings.poe_classifier_bot, messages, temperature=0.0, max_tokens=1200
        )
        result = parse_json(raw)
        if result is None:
            logger.warning("Классификатор вернул не JSON: %r", raw[:200])
            return None
        try:
            confidence = float(result.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        result["confidence"] = confidence
        result["should_clarify"] = confidence < settings.confidence_threshold
        if not isinstance(result.get("extracted"), dict):
            result["extracted"] = {}
        return result
