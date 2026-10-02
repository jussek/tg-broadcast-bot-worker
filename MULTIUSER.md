# Multi-user Telegram account architecture

## Goal

Every person who starts the bot owns an isolated workspace:

- their Telegram account/session;
- their live Telegram groups and channels;
- their saved group lists;
- their templates and last message;
- their timers and delivery results.

A broadcast task always sends from the Telegram account owned by `task.user_id`.
There is no shared `TELEGRAM_SESSION_STRING` fallback for user tasks.

## Account connection

The bot exposes `👤 Telegram-аккаунт`. When the user chooses to connect:

1. The server creates a cryptographically random one-time challenge tied to the Bot API user id.
2. The HTTPS page opens an unauthenticated Telethon connection and requests Telegram QR login.
3. The page shows Telegram's short-lived `tg://login?token=...` link.
4. The user confirms the new session in an already-authorized official Telegram client.
5. After Telegram confirms the QR token, the server reads `get_me()` and requires its Telegram user id to equal the Bot API user id that created the challenge.
6. The resulting `StringSession` is encrypted before being persisted.

The application does **not** ask users to paste Telegram login codes or 2FA passwords into the bot or website.

Accounts with Telegram two-step verification currently stop at the safe QR stage if Telethon requests an account password. The service does not collect that password.

## Session storage

Redis key:

`broadcast:telegram-account:{bot_user_id}`

The record contains public account metadata and `session_ciphertext`. Plaintext `StringSession` values are never stored.

Encryption:

- preferred: `SESSION_ENCRYPTION_KEY` (Fernet key);
- rollout fallback: a deterministic dedicated key derived from server-only `WEBHOOK_SETUP_SECRET`.

Configure a dedicated `SESSION_ENCRYPTION_KEY` before rotating `WEBHOOK_SETUP_SECRET`.

Connection challenges are encrypted as well and expire after 15 minutes. Telegram's QR authorization token itself is much shorter-lived.

## Worker request flow

```text
QStash /api/process
  -> task_id
  -> Redis task
  -> task.user_id
  -> encrypted personal Telegram session
  -> decrypt in server process
  -> TelegramClient(StringSession(...))
  -> send only to task.groups
```

If the owner's session is missing or invalid, the task is moved to `error` and is not silently executed with another account.

## Existing user data isolation

The existing bot storage already namespaces application state by Telegram Bot API user id:

- `broadcast:state:{user_id}`
- `broadcast:templates:{user_id}`
- `broadcast:group-lists:{user_id}`
- `broadcast:groups:{user_id}`
- `broadcast:user:{user_id}:tasks`

Task records also contain `user_id`. The multi-user change extends the same ownership boundary to the Telethon session.

## Disconnect behavior

Disconnecting a Telegram account:

- deletes only that user's encrypted Telegram session;
- clears only that user's live-group cache;
- cancels that user's active timers;
- leaves other users untouched.

## Legacy session

`TELEGRAM_SESSION_STRING` remains an optional compatibility variable for local/admin calls that explicitly omit `user_id`. Production user tasks resolve a personal session and never use the shared legacy value.
