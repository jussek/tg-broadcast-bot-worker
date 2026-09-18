# Telegram Broadcast Bot

Система массовой рассылки сообщений в Telegram через MTProto (Telethon).

## Архитектура

```
┌─────────────┐      HTTP       ┌──────────────────┐
│   Vercel    │ ◄─────────────► │  Worker (Render) │
│  FastAPI    │                 │   Telethon       │
│  (api/)     │                 │   aiohttp        │
│  + Bot      │                 │   Scheduler      │
└──────┬──────┘                 └────────┬─────────┘
       │                                  │
       │         ┌────────────┐           │
       ├────────►│  Supabase  │◄──────────┘
       │         │ PostgreSQL │
       │         └────────────┘
       │
       │         ┌────────────┐
       └────────►│ Upstash    │
                 │ Redis      │
                 └────────────┘
```

### Компоненты

| Компонент | Файл | Назначение |
|-----------|------|------------|
| **Vercel API** | `api/index.py` | FastAPI: CRUD операции, webhook для бота, проксирование к Worker |
| **Telegram Bot** | `bot.py` | Aiogram 3.x бот с inline-кнопками и командами (polling или webhook) |
| **Worker** | `worker.py` | Persistent процесс: Telethon, scheduler, HTTP сервер |
| **Supabase** | `storage/supabase_storage.py` | PostgreSQL: пользователи, шаблоны, задачи, логи |
| **Upstash Redis** | `redis_storage.py`, `worker.py` | Блокировки задач (locks), кэш |

## Быстрый старт

### 1. Supabase

1. Создайте проект на https://supabase.com
2. В SQL Editor выполните `supabase_schema.sql`
3. Скопируйте `SUPABASE_URL` и `SUPABASE_SERVICE_ROLE_KEY`

### 2. Upstash Redis

1. Создайте базу на https://upstash.com
2. Скопируйте REST URL и токен

### 3. Генерация SESSION_STRING

```bash
python generate_session.py
```

Введите API ID и API Hash из https://my.telegram.org/apps

### 4. Worker (Render/Railway/VPS)

**Переменные окружения:**
- `TELEGRAM_API_ID` — числовой ID приложения
- `TELEGRAM_API_HASH` — хэш приложения
- `TELEGRAM_SESSION_STRING` — строка сессии (только здесь!)
- `UPSTASH_REDIS_REST_URL` — URL Redis (опционально для одного экземпляра Worker; обязателен для нескольких реплик)
- `UPSTASH_REDIS_REST_TOKEN` — токен Redis (задаётся вместе с URL)
- `WORKER_SECRET` — случайный секрет для авторизации запросов
- `SUPABASE_URL` — URL проекта Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — сервисный ключ
- `PORT` — порт (по умолчанию 8080)

**Запуск:**
```bash
pip install -r requirements.txt
python worker.py
```

Worker запустит:
- Telegram клиент (Telethon)
- HTTP сервер на указанном порту
- Scheduler для периодических задач

**Health check:** `GET /health`

### 5. Vercel API

**Переменные окружения (без TELEGRAM_SESSION_STRING!):**
- `TELEGRAM_WORKER_URL` — URL вашего воркера (например, `https://your-worker.onrender.com`)
- `WORKER_SECRET` — тот же секрет, что у воркера
- `SUPABASE_URL` — URL проекта Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — сервисный ключ
- `TELEGRAM_BOT_TOKEN` — токен Telegram бота (от @BotFather)
- `VERCEL_API_URL` — URL вашего Vercel приложения

**Деплой:**
```bash
vercel --prod
```

### 6. Telegram Bot

**Запуск бота:**
```bash
export TELEGRAM_BOT_TOKEN="your_bot_token"
export VERCEL_API_URL="https://your-vercel-app.vercel.app"
python bot.py
```

Или используйте webhook режим через Vercel.

## API Endpoints

### Пользователи
- `POST /users` — создать пользователя
- `GET /users/{user_id}` — получить пользователя

### Шаблоны
- `POST /users/{user_id}/templates` — создать шаблон
- `GET /users/{user_id}/templates` — список шаблонов
- `PUT /templates/{template_id}` — обновить шаблон
- `DELETE /templates/{template_id}` — удалить шаблон

### Группы чатов
- `POST /users/{user_id}/group-sets` — создать набор групп
- `GET /users/{user_id}/group-sets` — список наборов
- `PUT /group-sets/{set_id}` — обновить набор
- `DELETE /group-sets/{set_id}` — удалить набор

### Задачи рассылок
- `POST /users/{user_id}/broadcast-tasks` — создать задачу
- `GET /users/{user_id}/broadcast-tasks` — список задач
- `DELETE /broadcast-tasks/{task_id}` — удалить задачу

### Отправка сообщений
- `POST /send` — отправить сообщения (через воркер)
- `GET /chats` — получить список чатов (через воркер)

### Логи
- `GET /users/{user_id}/logs` — история отправок

### Webhook
- `POST /webhook` — Telegram webhook для бота

## Безопасность

1. **SESSION_STRING** хранится только на воркере, никогда не передаётся в Vercel
2. **WORKER_SECRET** используется для авторизации запросов между Vercel и воркером
3. **RLS политики** в Supabase ограничивают доступ пользователей к их данным

## Важные замечания

⚠️ **Один экземпляр воркера**: Запускайте `worker.py` только на одном хосте. 
Несколько экземпляров с одной сессией приведут к конфликтам IP и бану.

⚠️ **Лимиты Telegram**: 
- Максимальная длина сообщения: 4096 символов
- Лимиты на отправку зависят от возраста аккаунта

⚠️ **Scheduler**: Работает только на persistent Worker, не на Vercel.

## Повторяющиеся рассылки

При создании задачи указывается:
- `interval_minutes` — интервал между повторами в минутах
- `repeats` — общее количество повторов

Пример: `interval_minutes=60`, `repeats=5` означает:
- Первая отправка через 60 минут после создания
- Затем ещё 4 отправки с интервалом 60 минут
- После 5-й отправки статус меняется на `completed`

## Лицензия

MIT
