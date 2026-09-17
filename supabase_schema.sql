-- Supabase SQL Schema for Telegram Broadcast Bot
-- Run this in your Supabase project's SQL Editor
-- This script is idempotent (safe to run multiple times)

-- =========================================================
-- USERS TABLE
-- =========================================================
CREATE TABLE IF NOT EXISTS public.users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,
    last_active_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_is_active ON public.users(is_active);

-- =========================================================
-- TEMPLATES TABLE
-- =========================================================
CREATE TABLE IF NOT EXISTS public.templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    message TEXT NOT NULL,
    groups JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_templates_user_id ON public.templates(user_id);
CREATE INDEX IF NOT EXISTS idx_templates_created_at ON public.templates(created_at DESC);

-- =========================================================
-- GROUP SETS TABLE (объединения чатов)
-- =========================================================
CREATE TABLE IF NOT EXISTS public.group_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    groups JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_group_sets_user_id ON public.group_sets(user_id);
CREATE INDEX IF NOT EXISTS idx_group_sets_created_at ON public.group_sets(created_at DESC);

-- =========================================================
-- BROADCAST TASKS TABLE (задачи рассылок)
-- =========================================================
CREATE TABLE IF NOT EXISTS public.broadcast_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    groups JSONB NOT NULL DEFAULT '[]'::jsonb,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    total_repeats INTEGER NOT NULL DEFAULT 1,
    completed_repeats INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', -- active, completed, cancelled, error
    next_run TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_user_id ON public.broadcast_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_status ON public.broadcast_tasks(status);
CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_next_run ON public.broadcast_tasks(next_run) WHERE status = 'active';

-- =========================================================
-- BROADCAST LOGS TABLE (история отправок)
-- =========================================================
CREATE TABLE IF NOT EXISTS public.broadcast_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES public.broadcast_tasks(id) ON DELETE SET NULL,
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    groups JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    errors JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broadcast_logs_user_id ON public.broadcast_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_logs_task_id ON public.broadcast_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_logs_created_at ON public.broadcast_logs(created_at DESC);

-- =========================================================
-- CHAT MEMBERSHIPS TABLE (какие чаты у пользователей)
-- =========================================================
CREATE TABLE IF NOT EXISTS public.chat_memberships (
    user_id BIGINT NOT NULL REFERENCES public.users(user_id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    chat_title TEXT NOT NULL,
    chat_type TEXT NOT NULL, -- 'group' or 'channel'
    last_seen TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_memberships_user_id ON public.chat_memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_memberships_chat_title ON public.chat_memberships(chat_title);

-- =========================================================
-- ROW LEVEL SECURITY (RLS)
-- =========================================================
-- Enable RLS on all tables
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.group_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.broadcast_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.broadcast_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.chat_memberships ENABLE ROW LEVEL SECURITY;

-- Drop existing policies if they exist (to allow re-running)
DROP POLICY IF EXISTS "Users can view their own data" ON public.users;
DROP POLICY IF EXISTS "Users can insert their own data" ON public.users;
DROP POLICY IF EXISTS "Users can update their own data" ON public.users;
DROP POLICY IF EXISTS "Service role full access users" ON public.users;

DROP POLICY IF EXISTS "Users can view their own templates" ON public.templates;
DROP POLICY IF EXISTS "Users can manage their own templates" ON public.templates;
DROP POLICY IF EXISTS "Service role full access templates" ON public.templates;

DROP POLICY IF EXISTS "Users can view their own group sets" ON public.group_sets;
DROP POLICY IF EXISTS "Users can manage their own group sets" ON public.group_sets;
DROP POLICY IF EXISTS "Service role full access group_sets" ON public.group_sets;

DROP POLICY IF EXISTS "Users can view their own tasks" ON public.broadcast_tasks;
DROP POLICY IF EXISTS "Users can manage their own tasks" ON public.broadcast_tasks;
DROP POLICY IF EXISTS "Service role full access broadcast_tasks" ON public.broadcast_tasks;

DROP POLICY IF EXISTS "Users can view their own logs" ON public.broadcast_logs;
DROP POLICY IF EXISTS "Service can insert logs" ON public.broadcast_logs;
DROP POLICY IF EXISTS "Service role full access broadcast_logs" ON public.broadcast_logs;

DROP POLICY IF EXISTS "Users can view their own chats" ON public.chat_memberships;
DROP POLICY IF EXISTS "Service can manage chats" ON public.chat_memberships;
DROP POLICY IF EXISTS "Service role full access chat_memberships" ON public.chat_memberships;

-- Policies for users table
-- Service role has full access
CREATE POLICY "Service role full access users" ON public.users
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

-- Users can only see/manage their own data (when authenticated via normal auth)
CREATE POLICY "Users can view their own data" ON public.users
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Users can insert their own data" ON public.users
    FOR INSERT WITH CHECK (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Users can update their own data" ON public.users
    FOR UPDATE USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

-- Policies for templates table
CREATE POLICY "Service role full access templates" ON public.templates
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Users can view their own templates" ON public.templates
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Users can manage their own templates" ON public.templates
    FOR ALL USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

-- Policies for group_sets table
CREATE POLICY "Service role full access group_sets" ON public.group_sets
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Users can view their own group sets" ON public.group_sets
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Users can manage their own group sets" ON public.group_sets
    FOR ALL USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

-- Policies for broadcast_tasks table
CREATE POLICY "Service role full access broadcast_tasks" ON public.broadcast_tasks
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Users can view their own tasks" ON public.broadcast_tasks
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Users can manage their own tasks" ON public.broadcast_tasks
    FOR ALL USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

-- Policies for broadcast_logs table
CREATE POLICY "Service role full access broadcast_logs" ON public.broadcast_logs
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Users can view their own logs" ON public.broadcast_logs
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Service can insert logs" ON public.broadcast_logs
    FOR INSERT WITH CHECK (true);

-- Policies for chat_memberships table
CREATE POLICY "Service role full access chat_memberships" ON public.chat_memberships
    FOR ALL USING (auth.jwt()->>'role' = 'service_role');

CREATE POLICY "Users can view their own chats" ON public.chat_memberships
    FOR SELECT USING (
        CASE 
            WHEN auth.jwt()->>'role' = 'service_role' THEN true
            ELSE (auth.jwt()->>'user_id')::bigint = user_id
        END
    );

CREATE POLICY "Service can manage chats" ON public.chat_memberships
    FOR ALL USING (true);

-- =========================================================
-- HELPER FUNCTIONS
-- =========================================================

-- Function to get stats for a user
CREATE OR REPLACE FUNCTION public.get_user_stats(p_user_id BIGINT)
RETURNS JSONB AS $$
DECLARE
    v_templates_count INTEGER;
    v_group_sets_count INTEGER;
    v_active_tasks_count INTEGER;
    v_total_broadcasts INTEGER;
    v_total_messages_sent INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_templates_count FROM public.templates WHERE user_id = p_user_id;
    SELECT COUNT(*) INTO v_group_sets_count FROM public.group_sets WHERE user_id = p_user_id;
    SELECT COUNT(*) INTO v_active_tasks_count FROM public.broadcast_tasks WHERE user_id = p_user_id AND status = 'active';
    SELECT COUNT(*) INTO v_total_broadcasts FROM public.broadcast_logs WHERE user_id = p_user_id;
    SELECT COALESCE(SUM(success_count), 0) INTO v_total_messages_sent FROM public.broadcast_logs WHERE user_id = p_user_id;
    
    RETURN jsonb_build_object(
        'templates_count', v_templates_count,
        'group_sets_count', v_group_sets_count,
        'active_tasks_count', v_active_tasks_count,
        'total_broadcasts', v_total_broadcasts,
        'total_messages_sent', v_total_messages_sent
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- =========================================================
-- SEED DATA (optional, for testing)
-- =========================================================

-- Uncomment to add test data
-- INSERT INTO public.users (user_id, username, first_name) VALUES (123456789, 'testuser', 'Test');
