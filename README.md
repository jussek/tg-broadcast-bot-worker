# Telegram Broadcast Bot

Система массовой рассылки сообщений в Telegram через MTProto (Telethon).

## Архитектура

```
┌─────────────┐      HTTP       ┌──────────────────┐
│   Vercel    │ ◄─────────────► │  Worker (Render) │
│  FastAPI    │                 │   Telethon       │
│  (api/)     │                 │   aiohttp        │
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
| **Vercel API** | `api/index.py` | FastAPI вебхук для Telegram бота, CRUD операции |
| **Worker** | `worker.py` | Единственный процесс с Telethon для отправки через MTProto |
| **Supabase** | `storage/supabase_storage.py` | PostgreSQL для пользователей, шаблонов, задач, логов |
| **Upstash Redis** | `redis_storage.py`, `worker_client.py` | Кэш, UI состояние, блокировки задач |
| **QStash** | (опционально) | Планировщик периодических рассылок |

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

### 4. Worker (Render/Railway)

**Переменные окружения:**
- `TELEGRAM_API_ID` — числовой ID приложения
- `TELEGRAM_API_HASH` — хэш приложения
- `TELEGRAM_SESSION_STRING` — строка сессии (только здесь!)
- `UPSTASH_REDIS_REST_URL` — URL Redis
- `UPSTASH_REDIS_REST_TOKEN` — токен Redis
- `WORKER_SECRET` — случайный секрет для авторизации запросов
- `SUPABASE_URL` — URL проекта Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — сервисный ключ

**Запуск:**
```bash
pip install -r requirements.txt
python worker.py
```

### 5. Vercel API

**Переменные окружения (без TELEGRAM_SESSION_STRING!):**
- `TELEGRAM_WORKER_URL` — URL вашего воркера (например, `https://your-worker.onrender.com`)
- `WORKER_SECRET` — тот же секрет, что у воркера
- `SUPABASE_URL` — URL проекта Supabase
- `SUPABASE_SERVICE_ROLE_KEY` — сервисный ключ
- `UPSTASH_REDIS_REST_URL` — URL Redis (опционально)
- `UPSTASH_REDIS_REST_TOKEN` — токен Redis (опционально)

**Деплой:**
```bash
vercel --prod
```

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

## Лицензия

MIT
