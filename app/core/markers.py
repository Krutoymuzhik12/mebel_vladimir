"""Служебные маркеры в ответе менеджера — клиент их не видит."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MARKER_RE = re.compile(r"\[\[\s*([A-ZА-ЯЁa-zа-яё_]+)\s*:?\s*([^\]]*)\]\]", re.I)

PRICE = "РАСЧЁТ"
SNOOZE = "ОТЛОЖИТЬ"
STAFF = "СОТРУДНИК"
OPERATOR = "ОПЕРАТОР"


@dataclass
class Markers:
    price_request: str | None = None
    snooze_days: int | None = None
    snooze_reason: str | None = None
    staff: list[str] = field(default_factory=list)
    operator: str | None = None
    unknown: list[str] = field(default_factory=list)

    def any(self) -> bool:
        return bool(
            self.price_request
            or self.snooze_days
            or self.staff
            or self.operator
            or self.unknown
        )


def extract(text: str) -> tuple[str, Markers]:
    """Вырезает маркеры из текста ответа, возвращает (чистый текст, маркеры)."""
    markers = Markers()

    def _consume(match: re.Match[str]) -> str:
        raw_name = match.group(1).upper()
        body = (match.group(2) or "").strip()
        key = raw_name.replace("Ё", "Е")
        if key in {PRICE, "РАСЧЕТ", "PRICE"}:
            markers.price_request = body or "клиент просит стоимость"
        elif key == SNOOZE:
            days = 30
            reason = body
            if "|" in body:
                left, _, right = body.partition("|")
                reason = right.strip()
                try:
                    days = int(re.sub(r"\D", "", left) or "30")
                except ValueError:
                    days = 30
            else:
                digits = re.sub(r"\D", "", body)
                if digits:
                    days = int(digits)
                    reason = ""
            markers.snooze_days = max(1, min(days, 365))
            markers.snooze_reason = reason or None
        elif key == STAFF:
            if body:
                markers.staff.append(body)
        elif key == OPERATOR:
            markers.operator = body or "эскалация"
        else:
            markers.unknown.append(raw_name)
        return ""

    clean = MARKER_RE.sub(_consume, text or "")
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, markers
