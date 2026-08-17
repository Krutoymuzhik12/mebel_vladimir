# Деплой на сервер

Ubuntu/Debian, проект живёт в `/opt/vladimir_mebel`, слушает `127.0.0.1:8080`,
наружу торчит nginx с https. Wazzup шлёт вебхуки **только на https**, поэтому
голого IP недостаточно — нужен домен и сертификат.

Свой домен не обязателен: подойдёт бесплатный `nip.io`, который резолвится в ваш
IP автоматически. Для IP `203.0.113.10` домен будет `203-0-113-10.nip.io`.

Ниже везде подставьте свои значения:

```bash
SERVER_IP=203.0.113.10
SERVER_DOMAIN=203-0-113-10.nip.io
```

---

## 1. Первичная установка (один раз)

```bash
apt update && apt install -y python3-venv python3-pip git nginx certbot python3-certbot-nginx

git clone https://github.com/Krutoymuzhik12/mebel_vladimir.git /opt/vladimir_mebel
cd /opt/vladimir_mebel
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt
```

### .env

`.env` в git не лежит — создайте на сервере:

```bash
cp /opt/vladimir_mebel/.env.example /opt/vladimir_mebel/.env
nano /opt/vladimir_mebel/.env
```

Обязательно заполнить: `WAZZUP_API_KEY`, `POE_API_KEY`, `WAZZUP_WEBHOOK_SECRET`
(любая длинная случайная строка), `PUBLIC_WEBHOOK_URL=https://<SERVER_DOMAIN>`.

Секрет можно сгенерировать так:

```bash
openssl rand -hex 24
```

### systemd

```bash
cp /opt/vladimir_mebel/deploy/osnova.service /etc/systemd/system/osnova.service
systemctl daemon-reload
systemctl enable --now osnova
systemctl status osnova --no-pager
```

### nginx + сертификат

```bash
sed "s/SERVER_DOMAIN/$SERVER_DOMAIN/" /opt/vladimir_mebel/deploy/nginx.conf \
  > /etc/nginx/sites-available/osnova
ln -sf /etc/nginx/sites-available/osnova /etc/nginx/sites-enabled/osnova
rm -f /etc/nginx/sites-enabled/default
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
cd /opt/vladimir_mebel && git pull && .venv/bin/pip install -r requirements.txt && systemctl restart osnova && systemctl status osnova --no-pager
```

Логи:

```bash
journalctl -u osnova -f
```

---

## 3. Тестовый режим: один канал

```bash
cd /opt/vladimir_mebel && .venv/bin/python -m scripts.wazzup_channels
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
cd /opt/vladimir_mebel && .venv/bin/python -m scripts.wazzup_webhook --set
```

Скрипт соберёт адрес вида `https://<домен>/webhooks/wazzup/<секрет>` из
`PUBLIC_WEBHOOK_URL` и `WAZZUP_WEBHOOK_SECRET`.

Посмотреть текущую подписку:

```bash
cd /opt/vladimir_mebel && .venv/bin/python -m scripts.wazzup_webhook
```

---

## 5. Проверка боем

1. `journalctl -u osnova -f`
2. Написать боту в тестовый канал с личного аккаунта.
3. В логе должно появиться: `wazzup raw payload` → `batch flush` → `wazzup sent`.

Если `skip (тестовый режим)` — channelId в `TEST_CHANNEL_IDS` не тот, возьмите
его из строки лога `raw payload`.

Если `wazzup send failed` — смотрите код ошибки: `401` неверный ключ,
`403` нет доступа к каналу.
