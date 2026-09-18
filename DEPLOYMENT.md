# Telegram Broadcast Bot - Полная инструкция по развертыванию

## Архитектура системы

```
Telegram User → Telegram Bot (aiogram 3.x) → Vercel FastAPI → Supabase PostgreSQL
                                              ↓
                                    Worker API (Render/Railway)
                                              ↓
                              Persistent Worker + Scheduler + Telethon
                                              ↓
                                         Telegram MTProto
```

## Компоненты

| Компонент | Где работает | Назначение |
|-----------|-------------|------------|
| **Vercel API** | Vercel (serverless) | FastAPI: CRUD операции, webhook для бота |
| **Telegram Bot** | Vercel (webhook) или локально (polling) | Aiogram 3.x интерфейс |
| **Worker** | Render/Railway/VPS (persistent) | Telethon, scheduler, HTTP сервер |
| **Supabase** | Supabase Cloud | PostgreSQL: пользователи, шаблоны, задачи, логи |
| **Redis** | Upstash | Блокировки задач (locks) |

---

## Шаг 1: Supabase

1. Создайте проект на https://supabase.com
2. В SQL Editor выполните содержимое файла `supabase_schema.sql`
3. Скопируйте:
   - `SUPABASE_URL` (например: `https://xxxxx.supabase.co`)
   - `SUPABASE_SERVICE_ROLE_KEY` (начинается с `eyJ...`)

---

## Шаг 2: Upstash Redis

1. Создайте базу на https://upstash.com
2. Выберите **REST API**
3. Скопируйте:
   - `UPSTASH_REDIS_REST_URL` (например: `https://xxxxx.upstash.io`)
   - `UPSTASH_REDIS_REST_TOKEN`

---

## Шаг 3: Telegram Credentials

### 3.1. Получите API ID и API Hash

1. Откройте https://my.telegram.org/apps
2. Войдите под своим номером
3. Создайте новое приложение
4. Скопируйте:
   - `TELEGRAM_API_ID` (число, например: `12345678`)
   - `TELEGRAM_API_HASH` (строка, например: `abc123def456...`)

### 3.2. Сгенерируйте SESSION_STRING

Выполните локально (не на сервере!):

```bash
python generate_session.py
```

Введите API ID и API Hash. Скопируйте полученную строку — это `TELEGRAM_SESSION_STRING`.

⚠️ **ВАЖНО**: `TELEGRAM_SESSION_STRING` используется ТОЛЬКО в Worker. Никогда не добавляйте его в Vercel!

### 3.3. Создайте бота (@BotFather)

1. Откройте @BotFather в Telegram
2. Отправьте `/newbot`
3. Придумайте имя и username
4. Скопируйте токен — это `TELEGRAM_BOT_TOKEN`

---

## Шаг 4: Worker Secret

Сгенерируйте случайный секрет для авторизации между Vercel и Worker:

```bash
openssl rand -hex 32
```

Это будет `WORKER_SECRET` (одинаковый для Vercel и Worker).

---

## Шаг 5: Деплой Worker (Render/Railway/VPS)

### Переменные окружения Worker:

| Переменная | Значение |
|-----------|----------|
| `TELEGRAM_API_ID` | Число из шага 3.1 |
| `TELEGRAM_API_HASH` | Хэш из шага 3.1 |
| `TELEGRAM_SESSION_STRING` | Строка из шага 3.2 |
| `UPSTASH_REDIS_REST_URL` | URL из шага 2 |
| `UPSTASH_REDIS_REST_TOKEN` | Токен из шага 2 |
| `SUPABASE_URL` | URL из шага 1 |
| `SUPABASE_SERVICE_ROLE_KEY` | Ключ из шага 1 |
| `WORKER_SECRET` | Секрет из шага 4 |
| `PORT` | `8080` (по умолчанию) |

> Redis можно не задавать для **одного** экземпляра Worker: в этом случае
> рассылки продолжат работать, но межэкземплярная блокировка отсутствует.
> Для нескольких реплик задайте обе переменные Redis.

### Запуск Worker:

```bash
# Установка зависимостей
pip install -r requirements.txt

# Запуск
python worker.py
```

Worker запустит:
- Telegram клиент (Telethon)
- HTTP сервер на порту 8080
- Scheduler для периодических задач

### Проверка Worker:

```bash
curl https://your-worker-url.onrender.com/health
```

Ожидаемый ответ:
```json
{
  "ok": true,
  "service": "telegram-worker",
  "telegram_connected": true,
  "telegram_authorized": true,
  "scheduler_running": true,
  "redis_configured": true
}
```

---

## Шаг 6: Деплой Vercel API

### Переменные окружения Vercel:

| Переменная | Значение |
|-----------|----------|
| `TELEGRAM_WORKER_URL` | URL вашего Worker (например: `https://your-worker.onrender.com`) |
| `WORKER_SECRET` | Тот же секрет из шага 4 |
| `SUPABASE_URL` | URL из шага 1 |
| `SUPABASE_SERVICE_ROLE_KEY` | Ключ из шага 1 |
| `TELEGRAM_BOT_TOKEN` | Токен бота из шага 3.3 |
| `TELEGRAM_WEBHOOK_SECRET` | Случайная строка для проверки запросов Telegram к webhook |
| `VERCEL_API_URL` | URL вашего Vercel приложения (например: `https://your-app.vercel.app`) |

⚠️ **НЕ ДОБАВЛЯЙТЕ** `TELEGRAM_SESSION_STRING` в Vercel!

### Деплой:

```bash
vercel --prod
```

---

## Шаг 7: Настройка Telegram Webhook

После деплоя Vercel настройте webhook:

```bash
curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://your-vercel-app.vercel.app/telegram-webhook"
```

Или через API Vercel:

```bash
curl -X POST "https://your-vercel-app.vercel.app/set-webhook"
```

Проверка статуса webhook:

```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getWebhookInfo"
```

---

## Шаг 8: Тестирование

### 1. Запустите бота

Откройте вашего бота в Telegram и отправьте `/start`.

Ожидаемый ответ:
```
👋 Привет, <ваше имя>!

Это система массовой рассылки Telegram.

Вы можете:
• Создавать шаблоны сообщений
• Управлять наборами групп
• Планировать рассылки
• Просматривать историю

Выберите действие:
[📋 Мои шаблоны] [📊 Группы] [📬 Рассылки] [📜 История] [🔄 Обновить чаты]
```

### 2. Синхронизируйте чаты

Нажмите "🔄 Обновить чаты" — Worker получит список ваших чатов из Telegram и сохранит в Supabase.

### 3. Создайте набор групп

Через API или бота создайте набор групп с выбранными chat_id.

### 4. Создайте рассылку

Создайте задачу рассылки:
- Сообщение
- Набор групп
- Интервал (минуты)
- Количество повторов

### 5. Проверьте выполнение

Scheduler на Worker автоматически выполнит задачу в указанное время. Результаты сохранятся в `broadcast_logs`.

---

## Переменные окружения

### Для Worker (.env.worker):

```bash
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123def456...
TELEGRAM_SESSION_STRING=1BVtsOKcBu...
UPSTASH_REDIS_REST_URL=https://xxxxx.upstash.io
UPSTASH_REDIS_REST_TOKEN=xxxxx
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJxxxxx...
WORKER_SECRET=your_random_secret_here
PORT=8080
```

### Для Vercel:

```bash
TELEGRAM_WORKER_URL=https://your-worker.onrender.com
WORKER_SECRET=your_random_secret_here
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJxxxxx...
TELEGRAM_BOT_TOKEN=1234567890:AABBccDDee...
VERCEL_API_URL=https://your-app.vercel.app
```

---

## API Endpoints

### Vercel API

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/` | Информация о сервисе |
| GET | `/health` | Health check |
| POST | `/users` | Создать пользователя |
| GET | `/users/{user_id}` | Получить пользователя |
| POST | `/users/{user_id}/templates` | Создать шаблон |
| GET | `/users/{user_id}/templates` | Получить шаблоны |
| PUT | `/templates/{template_id}` | Обновить шаблон |
| DELETE | `/templates/{template_id}` | Удалить шаблон |
| POST | `/users/{user_id}/group-sets` | Создать набор групп |
| GET | `/users/{user_id}/group-sets` | Получить наборы |
| PUT | `/group-sets/{set_id}` | Обновить набор |
| DELETE | `/group-sets/{set_id}` | Удалить набор |
| POST | `/users/{user_id}/broadcast-tasks` | Создать задачу |
| GET | `/users/{user_id}/broadcast-tasks` | Получить задачи |
| DELETE | `/broadcast-tasks/{task_id}` | Удалить задачу |
| POST | `/send` | Отправить сообщение (через Worker) |
| GET | `/chats` | Получить чаты (из Worker) |
| GET | `/users/{user_id}/logs` | Получить логи |
| POST | `/telegram-webhook` | Webhook для бота |
| POST | `/set-webhook` | Установить webhook |
| POST | `/delete-webhook` | Удалить webhook |

### Worker API

| Метод | Endpoint | Описание |
|-------|----------|----------|
| GET | `/health` | Health check Worker |
| POST | `/send` | Отправить сообщение |
| GET | `/chats` | Получить список чатов |

---

## Troubleshooting

### Worker не подключается к Telegram

1. Проверьте `TELEGRAM_SESSION_STRING` — возможно истёк срок действия
2. Сгенерируйте новую сессию: `python generate_session.py`
3. Перезапустите Worker

### Ошибки FloodWait

Worker автоматически обрабатывает `FloodWaitError`. Задачи будут выполнены после истечения таймаута.

### Задачи не выполняются

1. Проверьте логи Worker
2. Убедитесь, что Scheduler запущен (`scheduler_running: true` в `/health`)
3. Проверьте Redis locks

### Бот не отвечает

1. Проверьте webhook: `getWebhookInfo`
2. Убедитесь, что `TELEGRAM_BOT_TOKEN` правильный
3. Проверьте логи Vercel

---

## Безопасность

- Никогда не коммитьте `.env` файлы с секретами
- `TELEGRAM_SESSION_STRING` только в Worker
- Используйте разные сессии для разработки и продакшена
- RLS политики в Supabase ограничивают доступ к данным пользователей
