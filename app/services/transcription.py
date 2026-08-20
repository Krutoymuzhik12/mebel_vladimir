"""Транскрибация голосовых.

Порт из соседнего проекта Vladimir_mebel (services/transcription.py) — код там
обкатан на живых клиентах, менялся только источник файла: здесь это ссылка
contentUri из Wazzup, а не файл на диске.

Провайдеры (TRANSCRIPTION_PROVIDER, по умолчанию auto):
- local  — faster-whisper на CPU, бесплатно, без внешних запросов
- openai — Whisper API (OPENAI_API_KEY)
- poe    — бот на Poe с загрузкой файла (fastapi-poe + POE_API_KEY)
- auto   — local → openai → poe → заглушка
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

TRANSCRIBE_PROMPT = (
    "Транскрибируй это голосовое сообщение на русском языке. "
    "В ответе верни только текст сообщения, без комментариев."
)

_local_model = None


def _dedupe(text: str) -> str:
    """Страховка от удвоенной транскрибации: 'X X' или 'X.X.' -> 'X'."""
    t = text.strip()
    if len(t) < 10:
        return t
    half, rem = divmod(len(t), 2)
    if rem == 0 and t[:half].strip() == t[half:].strip():
        return t[:half].strip()
    a, b = t[: half + 1].strip(), t[half:].strip()
    if a == b:
        return a
    return t


def _pick_provider() -> str:
    configured = (settings.transcription_provider or "auto").strip().lower()
    if configured != "auto":
        return configured
    try:
        import faster_whisper  # noqa: F401

        return "local"
    except ImportError:
        pass
    if settings.openai_api_key:
        return "openai"
    if settings.poe_api_key:
        try:
            import fastapi_poe  # noqa: F401

            return "poe"
        except ImportError:
            pass
    return "stub"


def _local_sync(audio_path: Path) -> str:
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel

        logger.info("Загрузка локальной модели Whisper «%s»…", settings.whisper_model)
        _local_model = WhisperModel(
            settings.whisper_model, device="cpu", compute_type="int8"
        )
    segments, _info = _local_model.transcribe(str(audio_path), language="ru")
    return " ".join(seg.text.strip() for seg in segments).strip()


async def _transcribe_openai(audio_path: Path) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    with open(audio_path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1", file=f, language="ru"
        )
    return (result.text or "").strip()


def _maybe_convert_to_mp3(src: Path) -> Path:
    if src.suffix.lower() == ".mp3" or shutil.which("ffmpeg") is None:
        return src
    mp3_path = src.with_suffix(".mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), str(mp3_path)],
            capture_output=True,
            check=True,
            timeout=60,
        )
        return mp3_path
    except (subprocess.SubprocessError, OSError):
        logger.warning("Конвертация ffmpeg не удалась, отправляем файл как есть")
        return src


async def _transcribe_poe(audio_path: Path) -> str:
    import fastapi_poe as fp

    send_path = await asyncio.to_thread(_maybe_convert_to_mp3, audio_path)
    with open(send_path, "rb") as f:
        attachment = await fp.upload_file(
            file=f, file_name=send_path.name, api_key=settings.poe_api_key
        )
    message = fp.ProtocolMessage(
        role="user", content=TRANSCRIBE_PROMPT, attachments=[attachment]
    )
    parts: list[str] = []
    async for partial in fp.get_bot_response(
        messages=[message],
        bot_name=settings.poe_whisper_bot,
        api_key=settings.poe_api_key,
    ):
        if getattr(partial, "is_replace_response", False):
            parts = [partial.text]
        else:
            parts.append(partial.text)
    return "".join(parts).strip()


def provider() -> str:
    """Какой провайдер реально будет распознавать — видно в логе на старте."""
    return _pick_provider()


def warm_local_model() -> None:
    """Подгрузить Whisper заранее — первое голосовое без долгой паузы."""
    if _pick_provider() != "local":
        return
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel

        logger.info("Прогрев Whisper «%s»…", settings.whisper_model)
        _local_model = WhisperModel(
            settings.whisper_model, device="cpu", compute_type="int8"
        )


async def transcribe(audio_path: Path) -> str:
    """Текст голосового. Ошибки возвращаются строкой в квадратных скобках:
    вызывающий код узнаёт их по failed()."""
    name = _pick_provider()
    try:
        if name == "local":
            text = await asyncio.to_thread(_local_sync, audio_path)
        elif name == "openai":
            text = await _transcribe_openai(audio_path)
        elif name == "poe":
            text = await _transcribe_poe(audio_path)
        else:
            return (
                "[заглушка транскрибации: поставьте faster-whisper "
                "или задайте OPENAI_API_KEY / POE_API_KEY + fastapi-poe]"
            )
        text = _dedupe(text)
        return text or "[транскрибация вернула пустой ответ]"
    except Exception:
        logger.exception("Транскрибация не удалась (provider=%s)", name)
        return "[ошибка транскрибации]"


async def transcribe_bytes(data: bytes, suffix: str = ".ogg") -> str:
    if not data:
        return "[пустой аудиофайл]"
    if len(data) > settings.voice_max_bytes:
        return "[голосовое слишком длинное]"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"voice{suffix}"
        path.write_bytes(data)
        duration = await asyncio.to_thread(_probe_duration_sec, path)
        if duration is not None and duration > settings.voice_max_seconds:
            logger.info(
                "голосовое %.1f сек > лимита %.0f — отказ",
                duration,
                settings.voice_max_seconds,
            )
            return "[голосовое слишком длинное]"
        return await transcribe(path)


def _probe_duration_sec(path: Path) -> float | None:
    """Длительность через ffprobe, если есть. Иначе None — режем только по байтам."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if out.returncode != 0:
            return None
        return float((out.stdout or "").strip())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def failed(text: str) -> bool:
    """Вернулось служебное сообщение об ошибке, а не речь."""
    return not text or text.startswith("[")


# На тишине и шуме Whisper не молчит, а выдаёт заученные фразы из титров
# роликов, на которых учился. Клиент таких слов не говорил.
_HALLUCINATIONS = (
    "продолжение следует",
    "субтитры",
    "субтитров",
    "подписывайтесь на канал",
    "подпишись на канал",
    "спасибо за просмотр",
    "спасибо за внимание",
    "редактор субтитров",
    "корректор",
    "dimatorzok",
    "thank you",
    "thanks for watching",
    "you",
)

# Второй вид галлюцинации: вместо речи модель подписывает фоновый звук
# («СПОКОЙНАЯ МУЗЫКА», «(аплодисменты)»). В короткой реплике это значит,
# что речи там не было.
_SOUND_TAGS = (
    "музыка",
    "аплодисмент",
    "смех",
    "звук",
    "шум",
    "тишина",
    "гудок",
    "music",
    "applause",
    "silence",
    "laughter",
)

_LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")


def unclear(text: str) -> bool:
    """Речь не разобрать: битая запись, шум или галлюцинация модели.

    Отличается от failed(): там транскрибация честно не сработала, здесь она
    вернула текст, но верить ему нельзя. Ответить по такому тексту хуже, чем
    переспросить: клиент получит ответ на то, чего не говорил.
    """
    if failed(text):
        return True

    clean = " ".join(text.split()).strip(" .,!?…-")
    letters = _LETTERS_RE.findall(clean)

    # «Да», «Нет», «Ага» голосом почти не шлют, а вот огрызок шума — постоянно
    if len(letters) < 4:
        return True

    # Русская речь, распознанная латиницей или иероглифами, — верный признак,
    # что модель угадывала по шуму
    if len(_CYRILLIC_RE.findall(clean)) / len(letters) < 0.5:
        return True

    low = clean.lower()
    words = [w for w in re.split(r"\W+", low) if w]

    if any(low == h for h in _HALLUCINATIONS):
        return True
    if len(words) <= 6 and any(h in low for h in _HALLUCINATIONS):
        return True

    # Подпись фонового звука вместо речи
    if len(words) <= 4 and any(tag in low for tag in _SOUND_TAGS):
        return True

    # На битом звуке модель зацикливается: «Спасибо. Спасибо. Спасибо.»
    if 3 <= len(words) <= 8 and len(set(words)) == 1:
        return True

    return False
