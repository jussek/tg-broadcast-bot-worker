# Telegram Broadcast Bot — Vercel + Supabase + Telegram Worker + Redis

## Architecture

- **Vercel**: Telegram Bot webhook, UI, business logic.
- **Supabase**: PostgreSQL database for users, templates, group sets, broadcast tasks, logs, and chat memberships.
- **Upstash Redis**: real-time UI state (pagination, selected chats), locks for concurrent task processing, QStash deduplication.
- **Persistent worker** (`worker.py`): the ONLY process that owns the Telethon session and sends messages via MTProto.
- **QStash**: scheduled task execution (cron-like triggers for recurring broadcasts).

This architecture prevents Telegram's `authorization key was used under two different IP addresses simultaneously` error by ensuring only the persistent worker uses the MTProto session.

## 1. Supabase Setup

### 1.1 Create a Supabase project

1. Go to [supabase.com](https://supabase.com) and create a new project.
2. Wait for the database to be ready.

### 1.2 Run the SQL schema

1. Open the SQL Editor in your Supabase dashboard.
2. Copy the contents of `supabase_schema.sql` from this repository.
3. Paste and run the SQL to create all tables, indexes, and RLS policies.

### 1.3 Get Supabase credentials

In your Supabase project settings:
- **Project URL**: Found in Settings → API
- **Anon Public Key**: Found in Settings → API (for client-side use)
- **Service Role Key**: Found in Settings → API (for server-side use, keep secret!)

## 2. Generate a NEW Telethon session

The old session may already have been invalidated by Telegram. Generate a fresh session **once**, then put it only on the worker.

Do not put `TELEGRAM_SESSION_STRING` into Vercel.

### How to generate a new session:

1. Run the session generator script locally (on your computer, NOT on the server):

```bash
python generate_session.py
```

2. Enter your API credentials when prompted (get them from https://my.telegram.org)

3. Copy the generated SESSION_STRING and add it to your worker's environment variables

4. **Important**: Use this session string in ONLY ONE place - the persistent worker. Do not run multiple instances with the same session.

## 3. Worker deployment

Deploy this repository to Railway, Render, Fly.io or a VPS.

Worker environment:

```text
API_ID=
API_HASH=
TELEGRAM_SESSION_STRING=
WORKER_SECRET=<long random secret>
PORT=8080
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
```

Start command:

```bash
python worker.py
```

Health endpoint:

```text
/health
```

## 4. Vercel environment

Vercel needs:

```text
BOT_TOKEN=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
UPSTASH_REDIS_REST_URL=
UPSTASH_REDIS_REST_TOKEN=
APP_URL=https://your-project.vercel.app
TELEGRAM_WORKER_URL=https://your-worker.example.com
WORKER_SECRET=<same secret as worker>
QSTASH_TOKEN=
QSTASH_SECRET=
```

**Do not set `API_ID`, `API_HASH` or `TELEGRAM_SESSION_STRING` on Vercel.**

## 5. Chat groups / объединения

An объединение stores Telegram chat IDs only. It can contain groups, supergroups and channels. Multiple объединения can be selected together. The final recipient list is a set/union of IDs, so overlapping объединения never cause duplicate sends.

## 6. Data Storage Strategy

### Supabase (PostgreSQL) - Persistent Data
- **users**: User profiles and settings
- **templates**: Message templates with names
- **group_sets**: Predefined groups of chats (объединения)
- **broadcast_tasks**: Scheduled recurring broadcasts
- **broadcast_logs**: History of all broadcast attempts with success/failure counts
- **chat_memberships**: Which chats belong to which users (for analytics)

### Redis - Ephemeral/Real-time Data
- UI state during user sessions (pagination, selected chats)
- Task locks for preventing concurrent execution
- QStash deduplication keys

## 7. Important

Do not run another Telethon process with the same session. Only the persistent worker may use the session.

## 8. Development Notes

### Using Supabase in local development

Create a `.env` file with:

```text
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
```

### Migration from Redis-only to Supabase + Redis

The existing `redis_storage.py` continues to work for:
- Real-time UI state (`user_state_key`)
- Task locks (`acquire_task_lock`, `release_task_lock`)

New code should use `supabase_storage.py` for:
- Templates (`create_template`, `get_user_templates`, etc.)
- Group sets (`create_group_set`, `get_user_group_sets`, etc.)
- Broadcast tasks (`create_broadcast_task`, `get_due_broadcast_tasks`, etc.)
- Logs (`log_broadcast`, `get_user_broadcast_logs`, etc.)
