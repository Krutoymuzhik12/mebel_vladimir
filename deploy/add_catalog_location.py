"""Добавить раздачу catalog/ в конфиг nginx, не сломав то, что дописал certbot.

Перегенерировать конфиг из шаблона нельзя: certbot уже вписал туда блок с
сертификатом и редирект на https. Поэтому вставляем только недостающий
location, в тот server-блок, который слушает 443.

Скрипт идемпотентный: второй запуск ничего не меняет. Перед правкой кладёт
рядом .bak. Ничего не перезагружает — nginx -t и reload делаете сами.

    python3 deploy/add_catalog_location.py [путь_к_конфигу]
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

DEFAULT_CONF = Path("/etc/nginx/sites-available/osnova")
CATALOG_ROOT = "/var/opt/vladimir/catalog"
MARKER = "location ~ ^/catalog/"

BLOCK = f"""
    # Фото каталога: Wazzup скачивает вложение по ссылке, поэтому картинки
    # должны быть видны снаружи. Отдаёт nginx напрямую, мимо приложения.
    # Регулярка пускает только сами картинки — meta.json и листинг папок нет.
    location ~ ^/catalog/(.+\\.(?:jpe?g|png|webp))$ {{
        alias {CATALOG_ROOT}/$1;
        expires 7d;
        access_log off;
    }}
"""


def find_ssl_server_start(text: str) -> int | None:
    """Смещение сразу после `server_name ...;` в блоке с listen 443."""
    for match in re.finditer(r"\bserver\s*\{", text):
        start = match.end()
        # конец этого server-блока ищем по балансу скобок
        depth, i = 1, start
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block = text[start:i]
        if "listen 443" not in block:
            continue
        name = re.search(r"server_name[^;]*;", block)
        if name:
            return start + name.end()
        return start
    return None


def main() -> int:
    conf = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONF
    if not conf.is_file():
        print(f"Конфиг не найден: {conf}")
        return 1

    text = conf.read_text(encoding="utf-8")
    if MARKER in text:
        print("Раздача catalog/ уже настроена — ничего не меняю.")
        return 0

    pos = find_ssl_server_start(text)
    if pos is None:
        print(
            "Не нашёл server-блок с listen 443. Сертификат уже выпущен?\n"
            "Проверьте конфиг глазами, вставлять вслепую не буду."
        )
        return 1

    backup = conf.with_suffix(conf.suffix + ".bak")
    shutil.copy2(conf, backup)
    conf.write_text(text[:pos] + "\n" + BLOCK + text[pos:], encoding="utf-8")
    print(f"Готово. Копия старого конфига: {backup}")
    print("Дальше: nginx -t && systemctl reload nginx")
    return 0


if __name__ == "__main__":
    sys.exit(main())
