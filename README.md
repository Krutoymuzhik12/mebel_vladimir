# Владимир — ИИ-менеджер (основа)

- **Wazzup API** — слушаем входящие и отвечаем
- **MAX** — цена + Авито «посмотрел номер, не позвонил»
- **amoCRM** — вне кода (позже)
- **Vision** — поиск мебели по фото (DINOv2 + Qdrant)
- В репозитории лежит каталог с фото (`catalog/`)

Подробности: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Запуск каркаса

```bash
python -m pip install -r requirements.txt
copy .env.example .env
python -m app.main
```

Webhook: `POST /webhooks/wazzup` (заголовок `X-Wazzup-Secret`, если задан в `.env`).

## Каталог / Vision

```bash
python scripts/import_catalog_xlsx.py
python scripts/retry_missing_photos.py
set PYTHONPATH=.
python -m app.vision.index_catalog --force
python -m app.vision.index_missing
uvicorn app.vision.api:app --host 127.0.0.1 --port 8090
```

История диалога для Manager: **последние 40 сообщений**.
