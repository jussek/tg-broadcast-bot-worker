# Telegram Broadcast Bot — Vercel + Telegram Worker + Redis

## Architecture

- **Vercel**: Telegram Bot webhook, UI, templates, group sets, timers.
- **Upstash Redis**: persistent user state, templates, group sets and timer data.
- **Persistent worker** (`worker.py`): the ONLY process that owns the Telethon session and sends messages.

This prevents Telegram's `authorization key was used under two different IP addresses simultaneously` error caused by using one MTProto session from Vercel serverless instances and a local process at the same time.

## 1. Generate a NEW Telethon session

The old session may already have been invalidated by Telegram. Generate a fresh session **once**, then put it only on the worker.

Do not put `TELEGRAM_SESSION_STRING` into Vercel.

## 2. Worker deployment

Deploy this repository to Railway, Render, Fly.io or a VPS.

Worker environment:

```text
API_ID=
API_HASH=
TELEGRAM_SESSION_STRING=
WORKER_SECRET=<long random secret>
PORT=8080
```

Start command:

```bash
python worker.py
```

Health endpoint:

```text
/health
```

## 3. Vercel environment

Vercel needs:

```text
BOT_TOKEN=
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=
APP_URL=https://your-project.vercel.app
TELEGRAM_WORKER_URL=https://your-worker.example.com
WORKER_SECRET=<same secret as worker>
QSTASH_TOKEN=
QSTASH_SECRET=
```

**Do not set `API_ID`, `API_HASH` or `TELEGRAM_SESSION_STRING` on Vercel.**

## 4. Chat groups / объединения

An объединение stores Telegram chat IDs only. It can contain groups, supergroups and channels. Multiple объединения can be selected together. The final recipient list is a set/union of IDs, so overlapping объединения never cause duplicate sends.

## 5. Important

Do not run another Telethon process with the same session. Only the persistent worker may use the session.
