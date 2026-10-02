# Authentication and credential flow

This repository uses two different Telegram identities plus infrastructure credentials. They must not be mixed up.

## Components

### 1. Telegram Bot API identity

Environment variable: `BOT_TOKEN`.

Used by `api/index.py` to create the aiogram `Bot` that receives UI commands/callbacks and sends status/error notifications back to the owner.

This token represents the BotFather bot only. It does **not** grant permission to publish as the user's personal Telegram account.

### 2. Telegram MTProto user identity

Environment variables:

- `API_ID`
- `API_HASH`
- `TELEGRAM_SESSION_STRING`

Used by `services/telegram_delivery.py` / `services.broadcast_runner.py` through Telethon. This is the identity that enumerates the account's dialogs and sends broadcast messages.

`TELEGRAM_SESSION_STRING` is effectively a reusable login credential for the Telegram user account and must be treated like a password/session cookie. A fresh serverless invocation may not have Telethon's previous entity cache, so channel/supergroup sends must resolve an InputPeer/access_hash before sending. The delivery hardening layer in `services/delivery_fixes.py` now does that and refreshes dialogs only after an entity-cache miss.

### 3. Telegram webhook authentication

Environment variable: `TELEGRAM_WEBHOOK_SECRET`.

Telegram sends this value in `X-Telegram-Bot-Api-Secret-Token`. `/api/webhook` rejects the request with HTTP 403 when a configured secret does not match.

This protects incoming Bot API updates; it is unrelated to the MTProto user session used for broadcasts.

### 4. Maintenance/setup authentication

Environment variable: `WEBHOOK_SETUP_SECRET`.

Required in the `X-Setup-Secret` header for setup/maintenance endpoints such as webhook installation, webhook diagnostics, task cancellation and webhook deletion.

### 5. Redis authentication

Environment variables:

- `UPSTASH_REDIS_REST_URL`
- `UPSTASH_REDIS_REST_TOKEN`

Optional fallback: `REDIS_URL` when a compatible redis-py installation is available.

Redis stores user FSM/state, selected groups/cache, templates, tasks, runs, leases, per-send idempotency markers and QStash delivery dedupe markers. Telegram credentials are read from environment variables and are not intentionally written to Redis.

### 6. QStash publish authentication

Environment variable: `QSTASH_TOKEN`.

Used by `services/broadcast_runner.py` to publish delayed `/api/process` requests. This token authenticates the application to QStash when scheduling work.

### 7. QStash inbound request verification

The current `api/index.py` contains a legacy optional `QSTASH_VERIFICATION_KEY` verifier. Modern QStash signs requests using a JWT in the `Upstash-Signature` header with current/next signing keys. The legacy verifier should therefore be treated as a separate security-hardening item and migrated to the current QStash signing-key flow before relying on it as the only protection for `/api/process`.

## Request flow

```text
Telegram user
    |
    | command/callback
    v
Telegram Bot API
    |
    | POST /api/webhook
    | X-Telegram-Bot-Api-Secret-Token
    v
FastAPI / aiogram
    |
    | stores state/task in Redis
    | publishes delayed job with QSTASH_TOKEN
    v
QStash
    |
    | POST /api/process
    v
broadcast_runner.handle_delivery
    |
    | creates Telethon client using
    | API_ID + API_HASH + TELEGRAM_SESSION_STRING
    | resolves InputPeer/access_hash as needed
    v
Telegram MTProto
    |
    | send_message as the authorized user account
    v
target groups/channels
```

## Credential handling rules

- Keep all real credentials in Vercel/hosting secret environment variables, never in source files.
- Never commit `.env`, `*.session` or `*.session-journal`; `.gitignore` now explicitly excludes them while allowing `.env.example`.
- Rotate `BOT_TOKEN`, Redis/QStash tokens and Telegram user sessions if they are ever exposed.
- `TELEGRAM_SESSION_STRING` is especially sensitive: possession of it together with the API application credentials can allow reuse of the logged-in Telegram session.
- Do not log secret values. Existing request diagnostics log update/task identifiers, not credential contents.
