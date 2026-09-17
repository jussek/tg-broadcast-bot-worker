-- Supabase SQL Schema for Telegram Broadcast Bot
-- Run this in your Supabase project's SQL Editor

-- =========================================================
-- USERS TABLE
-- =========================================================
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    is_active BOOLEAN DEFAULT TRUE,
    last_active_at DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_users_is_active ON users(is_active);

-- =========================================================
-- TEMPLATES TABLE
-- =========================================================
CREATE TABLE IF NOT EXISTS templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    message TEXT NOT NULL,
    groups JSONB DEFAULT '[]'::jsonb,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_templates_user_id ON templates(user_id);
CREATE INDEX IF NOT EXISTS idx_templates_created_at ON templates(created_at DESC);

-- =========================================================
-- GROUP SETS TABLE (объединения чатов)
-- =========================================================
CREATE TABLE IF NOT EXISTS group_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    groups JSONB DEFAULT '[]'::jsonb,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_group_sets_user_id ON group_sets(user_id);
CREATE INDEX IF NOT EXISTS idx_group_sets_created_at ON group_sets(created_at DESC);

-- =========================================================
-- BROADCAST TASKS TABLE (задачи рассылок)
-- =========================================================
CREATE TABLE IF NOT EXISTS broadcast_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    groups JSONB NOT NULL DEFAULT '[]'::jsonb,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    total_repeats INTEGER NOT NULL DEFAULT 1,
    completed_repeats INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', -- active, completed, cancelled, error
    next_run DOUBLE PRECISION,
    last_error TEXT,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    updated_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_user_id ON broadcast_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_status ON broadcast_tasks(status);
CREATE INDEX IF NOT EXISTS idx_broadcast_tasks_next_run ON broadcast_tasks(next_run) WHERE status = 'active';

-- =========================================================
-- BROADCAST LOGS TABLE (история отправок)
-- =========================================================
CREATE TABLE IF NOT EXISTS broadcast_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID REFERENCES broadcast_tasks(id) ON DELETE SET NULL,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    groups JSONB NOT NULL DEFAULT '[]'::jsonb,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    errors JSONB DEFAULT '[]'::jsonb,
    created_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
);

CREATE INDEX IF NOT EXISTS idx_broadcast_logs_user_id ON broadcast_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_logs_task_id ON broadcast_logs(task_id);
CREATE INDEX IF NOT EXISTS idx_broadcast_logs_created_at ON broadcast_logs(created_at DESC);

-- =========================================================
-- CHAT MEMBERSHIPS TABLE (какие чаты у пользователей)
-- =========================================================
CREATE TABLE IF NOT EXISTS chat_memberships (
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    chat_id BIGINT NOT NULL,
    chat_title TEXT NOT NULL,
    chat_type TEXT NOT NULL, -- 'group' or 'channel'
    last_seen DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
    PRIMARY KEY (user_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_memberships_user_id ON chat_memberships(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_memberships_chat_title ON chat_memberships(chat_title);

-- =========================================================
-- ROW LEVEL SECURITY (RLS) - Optional but recommended
-- =========================================================

-- Enable RLS on all tables
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE group_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcast_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE broadcast_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_memberships ENABLE ROW LEVEL SECURITY;

-- Policies for users table
CREATE POLICY "Users can view their own data"
    ON users FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can insert their own data"
    ON users FOR INSERT
    WITH CHECK (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can update their own data"
    ON users FOR UPDATE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

-- Policies for templates table
CREATE POLICY "Users can view their own templates"
    ON templates FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can insert their own templates"
    ON templates FOR INSERT
    WITH CHECK (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can update their own templates"
    ON templates FOR UPDATE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can delete their own templates"
    ON templates FOR DELETE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

-- Policies for group_sets table
CREATE POLICY "Users can view their own group sets"
    ON group_sets FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can insert their own group sets"
    ON group_sets FOR INSERT
    WITH CHECK (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can update their own group sets"
    ON group_sets FOR UPDATE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can delete their own group sets"
    ON group_sets FOR DELETE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

-- Policies for broadcast_tasks table
CREATE POLICY "Users can view their own tasks"
    ON broadcast_tasks FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can insert their own tasks"
    ON broadcast_tasks FOR INSERT
    WITH CHECK (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can update their own tasks"
    ON broadcast_tasks FOR UPDATE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Users can delete their own tasks"
    ON broadcast_tasks FOR DELETE
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

-- Policies for broadcast_logs table
CREATE POLICY "Users can view their own logs"
    ON broadcast_logs FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Service can insert logs"
    ON broadcast_logs FOR INSERT
    WITH CHECK (true);

-- Policies for chat_memberships table
CREATE POLICY "Users can view their own chats"
    ON chat_memberships FOR SELECT
    USING (auth.jwt() ->> 'user_id')::BIGINT = user_id;

CREATE POLICY "Service can insert/update chats"
    ON chat_memberships FOR INSERT
    WITH CHECK (true);

CREATE POLICY "Service can update chats"
    ON chat_memberships FOR UPDATE
    USING (true);

CREATE POLICY "Service can delete chats"
    ON chat_memberships FOR DELETE
    USING (true);

-- =========================================================
-- HELPER FUNCTIONS
-- =========================================================

-- Function to get stats for a user
CREATE OR REPLACE FUNCTION get_user_stats(p_user_id BIGINT)
RETURNS JSONB AS $$
DECLARE
    v_templates_count INTEGER;
    v_group_sets_count INTEGER;
    v_active_tasks_count INTEGER;
    v_total_broadcasts INTEGER;
    v_total_messages_sent INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_templates_count FROM templates WHERE user_id = p_user_id;
    SELECT COUNT(*) INTO v_group_sets_count FROM group_sets WHERE user_id = p_user_id;
    SELECT COUNT(*) INTO v_active_tasks_count FROM broadcast_tasks WHERE user_id = p_user_id AND status = 'active';
    SELECT COUNT(*) INTO v_total_broadcasts FROM broadcast_logs WHERE user_id = p_user_id;
    SELECT COALESCE(SUM(success_count), 0) INTO v_total_messages_sent FROM broadcast_logs WHERE user_id = p_user_id;
    
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
-- INSERT INTO users (user_id, username, first_name) VALUES (123456789, 'testuser', 'Test');
