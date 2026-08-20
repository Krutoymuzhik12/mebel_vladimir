"""Подбор позиций каталога по словам клиента.

Без нейросети: 1625 позиций — это тот размер, где честный перебор с понятным
скорингом работает быстрее и предсказуемее эмбеддингов, а главное — его можно
объяснить, когда выдача не нравится.

Главное правило: сначала жёсткий фильтр по типу мебели, потом уже похожесть.
Клиент, спросивший про угловые диваны, не должен увидеть шкаф, даже если тот
идеально совпал по цвету.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.catalog import vocab

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = ROOT / "catalog" / "index.json"

# Слова, которые есть почти в каждой позиции и ничего не различают
STOPWORDS = frozenset({
    "на", "в", "и", "с", "для", "от", "до", "по", "из", "заказ", "мебель",
    "мебели", "руб", "см", "цена", "размеры", "комплектация", "цены",
    "характеристики", "наш", "адрес", "ваша", "актуальную", "стоимость",
    "наличие", "уточняйте", "менеджера", "есть", "какие", "бывают", "нужен",
    "нужна", "хочу", "покажите", "скиньте", "варианты", "вариант", "фото",
})

WORD_RE = re.compile(r"[а-яёa-z0-9]{3,}", re.I)


def tokens(text: str) -> set[str]:
    """Слова без окончаний: «диваны» из запроса должны встретиться
    с «диваном» из описания."""
    return {vocab.stem(w) for w in WORD_RE.findall((text or "").lower())
            if w not in STOPWORDS}


class CatalogSearch:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_INDEX
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            logger.warning("каталог не найден: %s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.exception("каталог не читается: %s", self.path)
            return
        if isinstance(data, list):
            self._items = data
            for item in self._items:
                item["_tokens"] = tokens(f"{item.get('name', '')} {item.get('description', '')}")
        logger.info("каталог загружен: %s позиций", len(self._items))

    @property
    def loaded(self) -> bool:
        return bool(self._items)

    @property
    def size(self) -> int:
        return len(self._items)

    @property
    def items(self) -> list[dict[str, Any]]:
        """Позиции каталога. Нужны отдаче фото: там ищут по артикулу."""
        return self._items

    def types(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for item in self._items:
            out[item.get("type", "?")] = out.get(item.get("type", "?"), 0) + 1
        return out

    # ---------- поиск ----------

    def search(
        self,
        text: str = "",
        *,
        furniture: str = "",
        colors: tuple[str, ...] | list[str] = (),
        budget: int = 0,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """Подходящие позиции. furniture — тип из фактов о клиенте."""
        if not self._items:
            return []

        wanted_type = vocab.type_from_client_words(furniture or "") or \
            vocab.type_from_client_words(text or "")
        wanted_features = set(vocab.detect_features(f"{furniture} {text}"))
        wanted_colors = {c.lower() for c in colors} | set(
            vocab.detect_colors(f"{furniture} {text}")
        )
        query_tokens = tokens(text) | tokens(furniture)

        # Без типа подборка вырождается в случайные позиции: на «нужен
        # аквариум» клиент получил бы три дивана. Честнее не показать ничего.
        if not wanted_type:
            logger.info("каталог: тип мебели не распознан — подборки не будет")
            return []
        pool = [i for i in self._items if i.get("type") == wanted_type]
        if not pool:
            logger.info("каталог: позиций типа %r нет", wanted_type)
            return []

        scored: list[tuple[float, dict[str, Any]]] = []
        for item in pool:
            score = self._score(
                item, query_tokens, wanted_features, wanted_colors, budget
            )
            if score > 0:
                scored.append((score, item))

        if not scored:
            # Тип совпал, но уточнения ни к чему не привели — покажем
            # что-нибудь из этого типа, начиная с недорогого
            scored = [(0.1, i) for i in pool]

        scored.sort(key=lambda pair: (-pair[0], pair[1].get("price") or 10**9))
        return self._diverse([i for _, i in scored], limit)

    def _score(
        self,
        item: dict[str, Any],
        query_tokens: set[str],
        wanted_features: set[str],
        wanted_colors: set[str],
        budget: int,
    ) -> float:
        score = 0.0
        features = set(item.get("features") or ())
        if wanted_features:
            hit = wanted_features & features
            score += 4.0 * len(hit)
            # Просили угловой, а он прямой — это не «чуть хуже», это не то
            if not hit:
                score -= 2.0

        if wanted_colors:
            item_colors = {c.lower() for c in (item.get("colors") or ())}
            score += 2.5 * len(wanted_colors & item_colors)

        if query_tokens:
            score += 1.0 * len(query_tokens & set(item.get("_tokens") or ()))

        price = item.get("price") or 0
        if budget and price:
            if price <= budget:
                score += 2.0
            elif price > budget * 1.3:
                score -= 2.0

        if item.get("photos"):
            score += 0.5
        return score

    @staticmethod
    def _diverse(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """Три варианта одной модели — плохая подборка.

        «Диван Моника», «Диван Моника с оттоманкой» и «Комплект Моника» для
        клиента одно и то же. Берём по одному представителю на модель.
        """
        out: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for item in items:
            words = [w for w in WORD_RE.findall((item.get("name") or "").lower())]
            model = next((w for w in words if w not in {"диван", "шкаф", "кухня",
                                                        "кровать", "комплект",
                                                        "мягкой", "угловой",
                                                        "модульная", "система"}), "")
            key = model or (item.get("name") or "")[:12].lower()
            if key in seen_models:
                continue
            seen_models.add(key)
            out.append(item)
            if len(out) >= limit:
                break
        return out


_shared: CatalogSearch | None = None


def shared() -> CatalogSearch:
    """Один загруженный каталог на процесс — файл читаем единожды."""
    global _shared
    if _shared is None:
        _shared = CatalogSearch()
    return _shared
