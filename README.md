# Telegram Broadcast Bot — Phase 1

Этот этап только авторизует твой личный Telegram-аккаунт через Telethon и создаёт StringSession.

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

## Что будет дальше

В следующем этапе мы добавим:

- получение списка групп;
- Telegram-бота;
- `/start`;
- кнопки;
- ввод сообщения;
- тестовую отправку;
- задержку между группами;
- сохранение последнего сообщения;
- таймеры и повторы;
- `/api/cron`;
- подключение внешнего cron;
- деплой на Vercel.
