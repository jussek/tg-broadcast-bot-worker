# Telegram Broadcast Bot

Бот отправляет сообщения от личного Telegram-аккаунта через Telethon, хранит
шаблоны и таймеры в Upstash Redis и обрабатывает Telegram webhook на Vercel.

## 1. Установить Python

Рекомендуется Python 3.11 или 3.12.

## 2. Создать виртуальное окружение

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Если команда `py -3.12` не работает:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 3. Установить зависимости

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Создать .env

Скопируй `.env.example` в `.env`:

```powershell
Copy-Item .env.example .env
```

Открой `.env` и укажи:

```text
API_ID=твой_api_id
API_HASH=твой_api_hash
PHONE=твой_номер_телефона
```

Не добавляй `.env` в GitHub.

## 5. Запустить авторизацию

```powershell
python auth_session.py
```

Telethon попросит код, который придёт в Telegram. Если включена двухэтапная аутентификация, будет также запрошен пароль.

После успешной авторизации появится:

```text
session_string.txt
```

Его содержимое — секрет. Не отправляй его в чат и не публикуй на GitHub.

## Деплой на Vercel

В Vercel добавь следующие переменные окружения для Production (и при
необходимости Preview):

```text
BOT_TOKEN=токен_бота_из_BotFather
API_ID=твой_api_id
API_HASH=твой_api_hash
TELEGRAM_SESSION_STRING=содержимое_session_string.txt
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
QSTASH_TOKEN=...
QSTASH_SECRET=длинный_случайный_секрет
TELEGRAM_WEBHOOK_SECRET=длинный_отдельный_случайный_секрет
APP_URL=https://имя-проекта.vercel.app
```

`APP_URL` должен быть публичным HTTPS-адресом именно production-деплоя, без
слеша в конце. При старте serverless-функции приложение само регистрирует этот
webhook. После первого деплоя открой `https://имя-проекта.vercel.app/`, чтобы
гарантированно запустить функцию. Если Telegram был временно недоступен или
webhook нужно перерегистрировать вручную, выполни:

```bash
curl -X POST "https://api.telegram.org/bot$BOT_TOKEN/setWebhook" \
  --data-urlencode "url=https://имя-проекта.vercel.app/api/webhook" \
  --data-urlencode "secret_token=$TELEGRAM_WEBHOOK_SECRET"
```

Проверь результат и адрес webhook:

```bash
curl "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"
```

Vercel направляет все URL в FastAPI-приложение через правило из `vercel.json`.
Без него Vercel ищет отдельные serverless-файлы для этих URL и webhook/QStash
не доходят до обработчиков.
