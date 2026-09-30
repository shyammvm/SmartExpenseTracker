-- ============================================================
-- Migration: Create dedicated credit_card_settings table
-- Run once in Supabase Dashboard → SQL Editor
-- ============================================================
-- Why a separate table is better:
--   1. budget_settings stores monthly salary history (one row per month).
--      Adding a global preference like cc_cycle_pct to it adds redundant
--      columns across all historical month rows.
--   2. credit_card_settings cleanly isolates credit card preferences
--      (CC spending limit % of salary, and future billing cycle configs)
--      into a guaranteed single-row table (id = 1).
--   3. Prevents any schema conflicts or broken queries when monthly
--      salaries are updated.
-- ============================================================

-- 1. Create credit_card_settings table
create table if not exists public.credit_card_settings (
    id              integer primary key default 1 check (id = 1),
    cc_cycle_pct    integer not null default 30 check (cc_cycle_pct between 1 and 80),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- 2. Seed initial default row (30% of salary)
insert into public.credit_card_settings (id, cc_cycle_pct)
values (1, 30)
on conflict (id) do nothing;

-- 3. Auto-update updated_at timestamp on edits (uses existing trigger function)
drop trigger if exists trg_credit_card_settings_updated_at on public.credit_card_settings;
create trigger trg_credit_card_settings_updated_at
    before update on public.credit_card_settings
    for each row
    execute function set_updated_at();

-- 4. Enable Row Level Security (matches existing tables)
alter table public.credit_card_settings enable row level security;
