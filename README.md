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

## Тестовый режим (сейчас включён)

Бот слушает **только** каналы из белого списка, остальные молча игнорирует.

```bash
# 1. узнать channelId нужного канала
python -m scripts.wazzup_channels

# 2. в .env
TEST_MODE=1
TEST_CHANNEL_IDS=<channelId телеграм-бота>
```

`TEST_CHAT_TYPES=telegram` — запасной вариант, если фильтровать по типу канала,
а не по конкретному UUID. Достаточно совпадения по любому из двух списков.

Выключить тестовый режим (слушать все каналы): `TEST_MODE=0`.

### Режим тишины

`WAZZUP_SEND_ENABLED=0` (значение по умолчанию) — ответ считается и пишется
в лог, но клиенту не уходит. Это же значение зашито дефолтом в коде: забытая
переменная в `.env` не приведёт к сообщениям в реальные каналы.

Включать отправку только осознанно: `WAZZUP_SEND_ENABLED=1`.

## Проверка без сервера

```bash
python -m scripts.selftest            # конвейер целиком, без сети
python -m scripts.selftest --with-poe # с реальным вызовом Poe
```

## Webhook

`POST /webhooks/wazzup/<WAZZUP_WEBHOOK_SECRET>` — рабочий путь: Wazzup не умеет
слать произвольные заголовки, поэтому секрет живёт в самом URL.
`POST /webhooks/wazzup` с заголовком `X-Wazzup-Secret` — для ручных тестов.

```bash
# сервис уже должен быть поднят и доступен снаружи по https
python -m scripts.wazzup_webhook        # показать текущую подписку
python -m scripts.wazzup_webhook --set  # записать PUBLIC_WEBHOOK_URL
```

Состояние сервиса: `GET /health` (видно ключи, тестовый режим, список каналов).

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
