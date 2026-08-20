# Деплой на сервер

Ubuntu/Debian, проект живёт в `/var/opt/vladimir`, слушает `127.0.0.1:8095`,
наружу торчит nginx с https. Wazzup шлёт вебхуки **только на https**, поэтому
голого IP недостаточно — нужен домен и сертификат.

Свой домен не обязателен: подойдёт бесплатный `nip.io`, который резолвится в ваш
IP автоматически. Для IP `203.0.113.10` домен будет `203-0-113-10.nip.io`.

Ниже везде подставьте свои значения (текущий сервер):

```bash
SERVER_IP=185.192.247.201
SERVER_DOMAIN=185-192-247-201.nip.io
```

## Если это переезд — прочитать до начала

Вебхук в Wazzup **один на аккаунт**. Как только зарегистрируете новый адрес,
старый сервер перестанет получать входящие. Но безвредным он не станет:

- фоновые **дожимы** продолжат уходить из его базы, и клиент получит
  напоминание дважды — от старого сервера и от нового;
- **опрос MAX** продолжится, и нажатие кнопки может уехать не в тот процесс.

Поэтому на старом сервере службу надо остановить, а не просто забыть про неё:

```bash
ssh root@217.149.23.80 'systemctl disable --now osnova; systemctl is-active osnova'
```

И забрать оттуда состояние — иначе на новом сервере текущие диалоги станут
незнакомыми, а история переписки потеряется:

```bash
scp root@217.149.23.80:/var/opt/vladimir_mebel/.env        /tmp/old.env
scp root@217.149.23.80:/var/opt/vladimir_mebel/data/bot.db /tmp/old_bot.db
```

`.env` ценен ключами (Wazzup, Poe, MAX, amoCRM), `bot.db` — статусами чатов и
историей. Оба положим на новое место ниже.

---

## 0. Режим тишины

Пока отправка не разрешена явно, в `.env` держим:

```
WAZZUP_SEND_ENABLED=0
```

Сервис считает ответ и пишет его в лог строкой `DRY-RUN ... наружу НЕ отправлено`,
но в канал не уходит ничего. Это же значение стоит дефолтом в коде: если
переменную забыли — сервис всё равно молчит.

Включать отправку (`=1`) только после явного разрешения.

---

## 1. Первичная установка

Сначала посмотреть, что на машине уже есть — ставить будем только недостающее:

```bash
python3 --version; git --version; nginx -v; ss -tlnp | grep -E ':(80|443|8095)' || echo 'порты свободны'
```

Нужен Python 3.10 или новее: код использует синтаксис `X | None`.

```bash
apt update && apt install -y python3-venv python3-pip git nginx certbot python3-certbot-nginx

git clone https://github.com/Krutoymuzhik12/mebel_vladimir.git /var/opt/vladimir
cd /var/opt/vladimir
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

Клон тянет около 700 МБ: в репозитории лежат фото каталога от старой выгрузки.
Новый каталог ходит по прямым ссылкам VK и эти файлы не использует — если решим
их убрать из git, клон станет заметно быстрее.

Положить перенесённое со старого сервера (если это переезд):

```bash
mkdir -p /var/opt/vladimir/data
[ -f /tmp/old.env ]    && cp /tmp/old.env    /var/opt/vladimir/.env
[ -f /tmp/old_bot.db ] && cp /tmp/old_bot.db /var/opt/vladimir/data/bot.db
```

В перенесённом `.env` обязательно поправить адрес — он указывает на старый сервер:

```bash
sed -i "s|^PUBLIC_WEBHOOK_URL=.*|PUBLIC_WEBHOOK_URL=https://$SERVER_DOMAIN|" /var/opt/vladimir/.env
grep '^PUBLIC_WEBHOOK_URL=' /var/opt/vladimir/.env
```

### .env

`.env` в git не лежит — создайте на сервере (если не перенесли из старой версии):

```bash
cp /var/opt/vladimir/.env.example /var/opt/vladimir/.env
nano /var/opt/vladimir/.env
```

Обязательно заполнить: `WAZZUP_API_KEY`, `POE_API_KEY`, `WAZZUP_WEBHOOK_SECRET`
(любая длинная случайная строка), `PUBLIC_WEBHOOK_URL=https://<SERVER_DOMAIN>`.

Проверить, что отправка выключена: `WAZZUP_SEND_ENABLED=0`.

Секрет можно сгенерировать так:

```bash
openssl rand -hex 24
```

### systemd

```bash
cp /var/opt/vladimir/deploy/osnova.service /etc/systemd/system/osnova.service
systemctl daemon-reload
systemctl enable --now osnova
systemctl status osnova --no-pager
```

### nginx + сертификат

```bash
sed "s/SERVER_DOMAIN/$SERVER_DOMAIN/" /var/opt/vladimir/deploy/nginx.conf \
  > /etc/nginx/sites-available/osnova
ln -sf /etc/nginx/sites-available/osnova /etc/nginx/sites-enabled/osnova
# Чужие конфиги не трогаем: на сервере живут другие проекты
nginx -t && systemctl reload nginx

certbot --nginx -d "$SERVER_DOMAIN" --agree-tos -m you@example.com --redirect -n
```

Проверка снаружи:

```bash
curl https://$SERVER_DOMAIN/health
```

Должен вернуться JSON с `"ok":true`.

---

## 2. Обновление кода (каждый раз)

```bash
cd /var/opt/vladimir && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart osnova && systemctl status osnova --no-pager
```

Логи:

```bash
journalctl -u osnova -f
```

---

## 3. Тестовый режим: один канал

```bash
cd /var/opt/vladimir && .venv/bin/python -m scripts.wazzup_channels
```

Скопировать `channelId` нужного канала в `.env`:

```
TEST_MODE=1
TEST_CHANNEL_IDS=<channelId>
```

и `systemctl restart osnova`.

---

## 4. Вебхук Wazzup

API-ключ и вебхук — это **разные направления**:

| | Кто кому стучится | Чем настраивается |
|---|---|---|
| `WAZZUP_API_KEY` | мы → Wazzup (отправка ответов) | ключ в `.env` |
| Вебхук | Wazzup → мы (входящие) | регистрация URL, один раз |

Без вебхука бот не увидит ни одного входящего сообщения.

Регистрация (сервис уже должен быть поднят и доступен по https — Wazzup
проверяет URL сразу же):

```bash
cd /var/opt/vladimir && .venv/bin/python -m scripts.wazzup_webhook --set
```

Скрипт соберёт адрес вида `https://<домен>/webhooks/wazzup/<секрет>` из
`PUBLIC_WEBHOOK_URL` и `WAZZUP_WEBHOOK_SECRET`.

Посмотреть текущую подписку:

```bash
cd /var/opt/vladimir && .venv/bin/python -m scripts.wazzup_webhook
```

---

## 5. Проверка боем

1. `journalctl -u osnova -f`
2. Написать боту в тестовый канал с личного аккаунта.
3. В логе должно появиться: `wazzup raw payload` → `batch flush` → `DRY-RUN ...
   наружу НЕ отправлено` с текстом ответа.

Клиенту при этом ничего не приходит — так и должно быть, пока
`WAZZUP_SEND_ENABLED=0`.

Если `skip (тестовый режим)` — channelId в `TEST_CHANNEL_IDS` не тот, возьмите
его из строки лога `raw payload`.

Порт занят (`address already in use`) — выбрать свободный:

```bash
ss -tlnp | grep -E ':80(80|95|00)'
```

Когда отправку разрешат: поставить `WAZZUP_SEND_ENABLED=1`, перезапустить,
и в логе вместо `DRY-RUN` появится `wazzup sent`. Если `wazzup send failed` —
смотрите код: `401` неверный ключ, `403` нет доступа к каналу.
