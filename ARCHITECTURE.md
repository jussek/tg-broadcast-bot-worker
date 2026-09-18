# Telegram Broadcast Bot - Architecture & Deployment Guide

## ✅ Fixed Issues Summary

### Critical Problems Resolved:

1. **bot.py** - Complete rewrite with proper aiogram 3.x structure
   - Fixed duplicate `setup_bot()` functions
   - Proper lazy initialization (no crash without TELEGRAM_BOT_TOKEN)
   - All handlers properly registered within `if bot and dp:` block
   - Added type hints for all functions

2. **api/index.py** - Unified webhook handling
   - Removed duplicate manual webhook processing
   - Integrated aiogram Dispatcher for `/telegram-webhook`
   - Added `/set-webhook` and `/delete-webhook` endpoints
   - Added missing `/users/{user_id}/sync-chats` endpoint
   - Helper functions `worker_get_chats()` and `worker_send_message()` properly defined

3. **Architecture** - Single source of truth
   - Supabase = primary database (users, templates, tasks, logs)
   - Redis = locks and cache only (not duplicate storage)
   - One Telegram processing system (aiogram via webhook)

4. **Imports** - All verified working
   - `bot.py` ✓
   - `api/index.py` ✓
   - `worker_client.py` ✓
   - `storage/supabase_storage.py` ✓

---

## 🏗️ System Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────┐
│   Telegram  │────▶│  Vercel FastAPI  │────▶│   Supabase  │
│    Users    │     │   (Webhook API)  │     │   Database  │
└─────────────┘     └──────────────────┘     └─────────────┘
                           │                        │
                           │                        │
                           ▼                        ▼
                    ┌─────────────┐          ┌─────────────┐
                    │  Aiogram    │          │   Worker    │
                    │  Dispatcher │          │   Client    │
                    └─────────────┘          └─────────────┘
                                                    │
                                                    ▼
                                          ┌─────────────────┐
                                          │ Persistent      │
                                          │ Telegram Worker │
                                          │ (Telethon +     │
                                          │  Scheduler)     │
                                          └─────────────────┘
                                                    │
                                                    ▼
                                          ┌─────────────┐
                                          │   Telegram  │
                                          │  Groups/Ch  │
                                          └─────────────┘
```

### Flow:

1. **User → Bot**: User sends `/start` or clicks inline button
2. **Telegram → Vercel**: Webhook POST to `/telegram-webhook`
3. **Vercel → Aiogram**: Update passed to Dispatcher
4. **Aiogram → Handler**: Registered handler processes command
5. **Handler → Supabase**: Read/write user data via API
6. **API → Worker**: For chat sync/message sending
7. **Worker → Telegram**: Telethon sends messages to groups
8. **Worker → Supabase**: Scheduler updates task status

---

## 🔧 Environment Variables

### Vercel (API/Bot):
```bash
TELEGRAM_BOT_TOKEN=8897529259:AAEpmvr3aTyTjf_J66_xqBBEROlwxP_40vM
VERCEL_API_URL=https://tg-broadcast-bot-worker.vercel.app
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbG...
TELEGRAM_WORKER_URL=https://your-worker.railway.app
WORKER_SECRET=your-secret-key
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
```

### Worker (Render/Railway):
```bash
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abc123def456
TELEGRAM_SESSION_STRING=1BVtsOKcBu7...
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_ROLE_KEY=eyJhbG...
UPSTASH_REDIS_REST_URL=https://...
UPSTASH_REDIS_REST_TOKEN=...
WORKER_SECRET=your-secret-key
PORT=8080
```

⚠️ **NEVER** put `TELEGRAM_SESSION_STRING` on Vercel!

---

## 🚀 Deployment Steps

### 1. Supabase Setup
```sql
-- Run in Supabase SQL Editor:
-- Copy entire content of supabase_schema.sql
```

### 2. Generate Session String (LOCAL ONLY)
```bash
python3 generate_session.py
# Follow prompts to enter phone/code
# Copy the session string
```

### 3. Deploy Worker (Railway/Render)
```bash
# Set all Worker ENV variables
# Deploy: python worker.py
# Health check: GET /health
```

### 4. Deploy Vercel API
```bash
# Set all Vercel ENV variables
vercel deploy --prod
# Verify: curl https://your-app.vercel.app/health
```

### 5. Setup Telegram Webhook
```bash
# Option A: Via API endpoint
curl -X POST https://your-app.vercel.app/set-webhook

# Option B: Direct Telegram API
curl "https://api.telegram.org/botTOKEN/setWebhook?url=https://your-app.vercel.app/telegram-webhook"
```

### 6. Test Bot
```
1. Open Telegram
2. Find your bot
3. Send /start
4. Should see welcome message with inline buttons
```

---

## 📡 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Service info |
| GET | `/health` | Health check |
| POST | `/users` | Create user |
| GET | `/users/{id}` | Get user |
| POST | `/users/{id}/templates` | Create template |
| GET | `/users/{id}/templates` | Get templates |
| GET | `/templates/{id}` | Get template |
| PUT | `/templates/{id}` | Update template |
| DELETE | `/templates/{id}` | Delete template |
| POST | `/users/{id}/group-sets` | Create group set |
| GET | `/users/{id}/group-sets` | Get group sets |
| PUT | `/group-sets/{id}` | Update group set |
| DELETE | `/group-sets/{id}` | Delete group set |
| POST | `/users/{id}/broadcast-tasks` | Create broadcast task |
| GET | `/users/{id}/broadcast-tasks` | Get tasks |
| DELETE | `/broadcast-tasks/{id}` | Delete task |
| GET | `/users/{id}/logs` | Get broadcast logs |
| POST | `/users/{id}/sync-chats` | Sync chats |
| GET | `/chats` | Get chats from Worker |
| POST | `/send` | Send message via Worker |
| POST | `/telegram-webhook` | Telegram webhook |
| POST | `/set-webhook` | Set bot webhook |
| POST | `/delete-webhook` | Delete bot webhook |

---

## 🧪 Testing Checklist

### Bot Commands:
- [ ] `/start` - Welcome message + inline keyboard
- [ ] Click "📋 Мои шаблоны" - Shows templates list
- [ ] Click "📊 Группы" - Shows group sets
- [ ] Click "📬 Рассылки" - Shows broadcast tasks
- [ ] Click "📜 История" - Shows logs
- [ ] Click "🔄 Обновить чаты" - Syncs chats

### API Tests:
```bash
# Health
curl https://your-app.vercel.app/health

# Create user
curl -X POST https://your-app.vercel.app/users \
  -H "Content-Type: application/json" \
  -d '{"user_id": 123456789, "username": "test", "first_name": "Test"}'

# Get user
curl https://your-app.vercel.app/users/123456789
```

### Worker Tests:
```bash
# Health
curl https://your-worker.railway.app/health

# Get chats (with secret)
curl -H "X-Worker-Secret: your-secret" \
  https://your-worker.railway.app/chats
```

---

## 🐛 Troubleshooting

### Error 500 on Vercel
1. Check logs: `vercel logs --since=5m`
2. Verify TELEGRAM_BOT_TOKEN is set
3. Check imports compile correctly

### Bot not responding
1. Check webhook: `curl "https://api.telegram.org/botTOKEN/getWebhookInfo"`
2. Verify Vercel deployment is successful
3. Check bot.py imports without errors

### Worker not starting
1. Verify all ENV variables are set
2. Check session string is valid
3. Review worker logs for auth errors

### Database errors
1. Run supabase_schema.sql in SQL Editor
2. Verify SUPABASE_SERVICE_ROLE_KEY is correct
3. Check RLS policies allow service_role access

---

## 📝 Key Design Decisions

1. **Single Bot System**: Aiogram handles all Telegram updates via webhook
2. **Supabase First**: All persistent data in Supabase, not Redis
3. **Redis for Locks**: Only distributed locking and caching
4. **Persistent Worker**: Scheduler runs continuously on Railway/Render
5. **No QStash**: Built-in scheduler replaces external queue
6. **Type Safety**: Full type hints throughout codebase
