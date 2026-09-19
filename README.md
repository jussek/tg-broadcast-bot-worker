# Telegram Broadcast Bot

Бот для рассылки сообщений в Telegram группы через Vercel serverless функции.

## Архитектура

```
/workspace/
├── api/                    # FastAPI endpoints для Vercel
│   ├── index.py           # Основной handler (webhook + process)
│   └── cron.py            # Cron endpoint (устарел, используется QStash)
├── bot/                   # aiogram обработчики
│   ├── __init__.py
│   └── dispatcher.py      # Регистрация handlers
├── services/              # Бизнес-логика
│   ├── __init__.py
│   ├── telegram_service.py  # Telethon операции
│   └── scheduler_service.py # QStash планировщик
├── storage/               # Работа с данными
│   ├── __init__.py
│   ├── redis_client.py    # Upstash Redis клиент
│   └── models.py          # Модели данных и хелперы
├── config/                # Конфигурация
├── tests/                 # Тесты
├── .env.example           # Шаблон переменных окружения
├── requirements.txt       # Зависимости
└── vercel.json           # Конфигурация Vercel
```

## Быстрый старт

### 1. Получение токенов

1. **BOT_TOKEN**: Создайте бота в [@BotFather](https://t.me/BotFather)
2. **API_ID/API_HASH**: Зарегистрируйте приложение на [my.telegram.org](https://my.telegram.org/apps)
3. **TELEGRAM_SESSION_STRING**: Запустите локально:
   ```bash
   python generate_session.py
   ```
4. **UPSTASH_REDIS_***: Создайте базу на [upstash.com](https://upstash.com)
5. **QSTASH_TOKEN**: Активируйте QStash в панели Upstash

### 2. Настройка переменных окружения

Скопируйте `.env.example` в `.env` и заполните значения:

```bash
cp .env.example .env
```

### 3. Деплой на Vercel

```bash
# Установите Vercel CLI
npm i -g vercel

# Авторизуйтесь
vercel login

# Деплой
vercel --prod
```

Или подключите репозиторий GitHub в панели Vercel.

### 4. Настройка webhook

После деплоя установите webhook:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<your-app>.vercel.app/api/webhook"
```

## Переменные окружения

| Переменная | Обязательная | Описание |
|------------|--------------|----------|
| `BOT_TOKEN` | ✅ | Токен бота от @BotFather |
| `API_ID` | ✅ | API ID от my.telegram.org |
| `API_HASH` | ✅ | API Hash от my.telegram.org |
| `TELEGRAM_SESSION_STRING` | ✅ | Строка сессии Telethon |
| `UPSTASH_REDIS_REST_URL` | ✅ | URL Upstash Redis |
| `UPSTASH_REDIS_REST_TOKEN` | ✅ | Токен Upstash Redis |
| `QSTASH_TOKEN` | ✅ | Токен QStash |
| `APP_URL` | ❌ | URL приложения на Vercel |
| `TELEGRAM_WEBHOOK_SECRET` | ❌ | Секрет для проверки webhook |

## Функционал

- `/start`, `/s` — главное меню
- Создание рассылки с выбором сообщения
- Выбор групп через Telethon
- Шаблоны сообщений
- Создание и удаление шаблонов сообщений
- Немедленная отправка
- Отложенная рассылка через QStash с задаваемыми интервалом в минутах и количеством повторов
- Просмотр статуса и отмена таймеров
- Сохранение каналов в именованные списки и повторное использование списков для рассылок
- Просмотр статуса задач

## Исправленные проблемы

### Критическая ошибка QStash

**Было:**
```python
from qstash.client import Client
qstash = Client(token=QSTASH_TOKEN)
qstash.publish_json(...)
```

**Стало:**
```python
from qstash import QStash
qstash = QStash(token=QSTASH_TOKEN)
qstash.message.publish_json(...)
```

Требуется версия `qstash>=2.0.0`.

## Тестирование

```bash
# Проверка синтаксиса
python -m py_compile api/index.py

# Тест импортов
PYTHONPATH=/workspace python tests/test_imports.py

# Тест QStash API
python tests/test_qstash_api.py
```

## Лицензия

MIT
