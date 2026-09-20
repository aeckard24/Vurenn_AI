create extension if not exists pgcrypto;

create table if not exists public.conversations (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  title text not null default 'New conversation',
  model_id text not null default 'vurenn',
  project_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists conversations_user_updated_idx
  on public.conversations (user_id, updated_at desc);

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  description text not null default '' check (char_length(description) <= 500),
  instructions text not null default '' check (char_length(instructions) <= 6000),
  color text not null default 'blue'
    check (color in ('blue', 'violet', 'emerald', 'amber', 'rose', 'slate')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists projects_user_updated_idx
  on public.projects (user_id, updated_at desc);

create table if not exists public.messages (
  id text primary key,
  conversation_id text not null
    references public.conversations(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system', 'tool', 'status')),
  content text not null default '',
  status text not null
    check (status in ('pending', 'streaming', 'completed', 'stopped', 'failed')),
  attachments jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists messages_conversation_created_idx
  on public.messages (conversation_id, created_at);
create index if not exists messages_user_idx on public.messages (user_id);

create table if not exists public.message_feedback (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  message_id text not null references public.messages(id) on delete cascade,
  rating smallint not null check (rating in (-1, 1)),
  comment text not null default '' check (char_length(comment) <= 1000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, message_id)
);

create index if not exists message_feedback_created_idx
  on public.message_feedback (created_at desc);
alter table public.message_feedback enable row level security;
revoke all on public.message_feedback from public, anon, authenticated;
grant select, insert, update, delete on public.message_feedback to service_role;

create table if not exists public.abuse_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  category text not null check (char_length(category) between 1 and 80),
  content_hash text not null check (char_length(content_hash) = 64),
  request_id text,
  content text not null default '',
  ip_hash text,
  user_agent text,
  expires_at timestamptz not null default (now() + interval '90 days'),
  created_at timestamptz not null default now()
);
alter table public.abuse_events add column if not exists content text not null default '';
alter table public.abuse_events add column if not exists ip_hash text;
alter table public.abuse_events add column if not exists user_agent text;
alter table public.abuse_events add column if not exists expires_at timestamptz not null default (now() + interval '90 days');
create index if not exists abuse_events_user_created_idx
  on public.abuse_events (user_id, created_at desc);
alter table public.abuse_events enable row level security;
revoke all on public.abuse_events from public, anon, authenticated;
grant select, insert, delete on public.abuse_events to service_role;

create table if not exists public.subscriptions (
  user_id uuid primary key references auth.users(id) on delete cascade,
  plan_id text not null default 'free'
    check (plan_id in ('free', 'pro', 'premier')),
  status text not null default 'inactive',
  stripe_customer_id text,
  stripe_subscription_id text,
  current_period_end timestamptz,
  updated_at timestamptz not null default now()
);

create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  occupation text not null default '',
  goals text[] not null default '{}'::text[],
  response_style text not null default 'balanced',
  response_preferences jsonb not null default '{
    "format": "balanced",
    "formality": 50,
    "warmth": 65,
    "humor": 20,
    "creativity": 45,
    "verbosity": 50,
    "initiative": 55,
    "markdown": true,
    "emojis": false,
    "custom_instructions": "",
    "voice_id": "af_heart",
    "appearance": {
      "color_theme": "classic",
      "accent": "blue",
      "gradient": "solid",
      "atmosphere": "none",
      "bubble": "rounded",
      "font_size": "default"
    }
  }'::jsonb,
  onboarding_completed boolean not null default false,
  onboarding_skipped boolean not null default false,
  security_prompt_dismissed boolean not null default false,
  camera_unlock_enabled boolean not null default false,
  limited_test_mode boolean not null default false,
  beta_access boolean not null default false,
  beta_invited_at timestamptz,
  legal_version text,
  legal_accepted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.profiles
  add column if not exists limited_test_mode boolean not null default false;
alter table public.profiles
  add column if not exists beta_access boolean not null default false;
alter table public.profiles
  add column if not exists beta_invited_at timestamptz;
alter table public.profiles add column if not exists legal_version text;
alter table public.profiles add column if not exists legal_accepted_at timestamptz;
alter table public.profiles
  add column if not exists response_preferences jsonb not null default '{
    "format": "balanced",
    "formality": 50,
    "warmth": 65,
    "humor": 20,
    "creativity": 45,
    "verbosity": 50,
    "initiative": 55,
    "markdown": true,
    "emojis": false,
    "custom_instructions": "",
    "voice_id": "af_heart",
    "appearance": {
      "color_theme": "classic",
      "accent": "blue",
      "gradient": "solid",
      "atmosphere": "none",
      "bubble": "rounded",
      "font_size": "default"
    }
  }'::jsonb;

create table if not exists public.legal_consents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  policy_version text not null check (char_length(policy_version) between 1 and 40),
  terms_accepted boolean not null,
  privacy_accepted boolean not null,
  acceptable_use_accepted boolean not null,
  accepted_at timestamptz not null,
  acceptance_method text not null default 'signup_checkbox'
    check (acceptance_method in ('signup_checkbox', 'oauth_signup', 'policy_update')),
  created_at timestamptz not null default now(),
  unique (user_id, policy_version)
);
create index if not exists legal_consents_user_created_idx
  on public.legal_consents (user_id, created_at desc);
alter table public.legal_consents enable row level security;
revoke all on public.legal_consents from public, anon, authenticated;
grant select, insert, update on public.legal_consents to service_role;

create table if not exists public.private_beta_invites (
  id uuid primary key default gen_random_uuid(),
  token_hash text not null unique check (char_length(token_hash) = 64),
  label text not null default '' check (char_length(label) <= 80),
  email text check (email is null or char_length(email) <= 320),
  expires_at timestamptz not null,
  max_uses integer not null default 1 check (max_uses between 1 and 50),
  use_count integer not null default 0 check (use_count between 0 and max_uses),
  revoked_at timestamptz,
  created_by uuid not null references auth.users(id) on delete restrict,
  created_at timestamptz not null default now()
);
create index if not exists private_beta_invites_created_idx
  on public.private_beta_invites (created_at desc);

create table if not exists public.private_beta_redemptions (
  invite_id uuid not null references public.private_beta_invites(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  redeemed_at timestamptz not null default now(),
  primary key (invite_id, user_id),
  unique (user_id)
);

alter table public.private_beta_invites enable row level security;
alter table public.private_beta_redemptions enable row level security;
revoke all on public.private_beta_invites from public, anon, authenticated;
revoke all on public.private_beta_redemptions from public, anon, authenticated;
grant select, insert, update, delete on public.private_beta_invites to service_role;
grant select, insert, update, delete on public.private_beta_redemptions to service_role;

create or replace function public.redeem_private_beta_invite(
  p_token_hash text,
  p_user_id uuid,
  p_user_email text
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  selected public.private_beta_invites%rowtype;
  already_redeemed boolean;
begin
  select * into selected
  from public.private_beta_invites
  where token_hash = p_token_hash
  for update;

  if not found then
    return jsonb_build_object('ok', false, 'code', 'invalid_invite');
  end if;
  if selected.revoked_at is not null then
    return jsonb_build_object('ok', false, 'code', 'revoked_invite');
  end if;
  if selected.expires_at <= now() then
    return jsonb_build_object('ok', false, 'code', 'expired_invite');
  end if;
  if selected.email is not null
     and lower(selected.email) <> lower(coalesce(p_user_email, '')) then
    return jsonb_build_object('ok', false, 'code', 'email_mismatch');
  end if;

  select exists(
    select 1 from public.private_beta_redemptions
    where invite_id = selected.id and user_id = p_user_id
  ) into already_redeemed;

  if not already_redeemed and selected.use_count >= selected.max_uses then
    return jsonb_build_object('ok', false, 'code', 'invite_full');
  end if;

  if not already_redeemed then
    insert into public.private_beta_redemptions (invite_id, user_id)
    values (selected.id, p_user_id);
    update public.private_beta_invites
    set use_count = use_count + 1
    where id = selected.id;
  end if;

  insert into public.profiles (user_id, display_name, beta_access, beta_invited_at)
  values (p_user_id, '', true, now())
  on conflict (user_id) do update
  set beta_access = true,
      beta_invited_at = coalesce(public.profiles.beta_invited_at, now()),
      updated_at = now();

  return jsonb_build_object('ok', true, 'label', selected.label);
end;
$$;

revoke all on function public.redeem_private_beta_invite(text, uuid, text)
  from public, anon, authenticated;
grant execute on function public.redeem_private_beta_invite(text, uuid, text)
  to service_role;

create table if not exists public.api_keys (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 80),
  key_prefix text not null,
  key_hash text not null unique,
  scopes text[] not null default array['chat:write']::text[],
  last_used_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists api_keys_user_created_idx
  on public.api_keys (user_id, created_at desc);
create index if not exists api_keys_active_hash_idx
  on public.api_keys (key_hash) where revoked_at is null;

alter table public.api_keys enable row level security;
revoke all on public.api_keys from public, anon, authenticated;
grant select, insert, update, delete on public.api_keys to service_role;

create table if not exists public.credit_accounts (
  user_id uuid primary key references auth.users(id) on delete cascade,
  balance integer not null default 2000 check (balance >= 0),
  lifetime_granted integer not null default 2000 check (lifetime_granted >= 0),
  lifetime_spent integer not null default 0 check (lifetime_spent >= 0),
  updated_at timestamptz not null default now()
);

create table if not exists public.credit_ledger (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  delta integer not null,
  balance_after integer not null check (balance_after >= 0),
  event_type text not null,
  feature_id text,
  idempotency_key text not null unique,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists credit_ledger_user_created_idx
  on public.credit_ledger (user_id, created_at desc);

create table if not exists public.credit_purchases (
  stripe_session_id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  pack_id text not null check (pack_id in ('credits_50', 'credits_100')),
  credits integer not null check (credits > 0),
  amount_cents integer not null check (amount_cents > 0),
  status text not null default 'completed',
  created_at timestamptz not null default now()
);

create table if not exists public.app_settings (
  key text primary key,
  value jsonb not null default '{}'::jsonb,
  updated_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now()
);

create table if not exists public.uploaded_files (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null,
  mime_type text not null,
  size integer not null check (size > 0),
  created_at timestamptz not null default now()
);

alter table public.uploaded_files
  add column if not exists project_id text;

create index if not exists uploaded_files_user_created_idx
  on public.uploaded_files (user_id, created_at desc);

alter table public.uploaded_files enable row level security;
alter table public.projects enable row level security;
revoke all on public.uploaded_files from public, anon, authenticated;
revoke all on public.projects from public, anon, authenticated;
grant select, insert, update, delete on public.uploaded_files to service_role;
grant select, insert, update, delete on public.projects to service_role;

alter table public.credit_accounts alter column balance set default 2000;
alter table public.credit_accounts alter column lifetime_granted set default 2000;

do $$
begin
  if not exists (
    select 1 from public.app_settings where key = 'credit_scale_v2'
  ) then
    update public.credit_accounts
    set balance = balance * 100,
        lifetime_granted = lifetime_granted * 100,
        lifetime_spent = lifetime_spent * 100;
    update public.credit_ledger
    set delta = delta * 100,
        balance_after = balance_after * 100;
    update public.credit_purchases set credits = credits * 100;
    insert into public.app_settings (key, value)
    values ('credit_scale_v2', '{"factor":100,"welcome_credits":2000}'::jsonb);
  end if;
end;
$$;

insert into public.app_settings (key, value)
values ('construction_mode', '{"enabled": false}'::jsonb)
on conflict (key) do nothing;

create or replace function public.initialize_vurenn_user()
returns trigger
language plpgsql
security definer set search_path = public
as $$
begin
  insert into public.profiles (user_id, display_name, legal_version, legal_accepted_at)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'display_name', split_part(new.email, '@', 1)),
    nullif(new.raw_user_meta_data->>'legal_version', ''),
    nullif(new.raw_user_meta_data->>'legal_accepted_at', '')::timestamptz
  )
  on conflict (user_id) do nothing;

  if coalesce((new.raw_user_meta_data->>'terms_accepted')::boolean, false)
     and coalesce((new.raw_user_meta_data->>'privacy_accepted')::boolean, false)
     and coalesce((new.raw_user_meta_data->>'acceptable_use_accepted')::boolean, false)
     and nullif(new.raw_user_meta_data->>'legal_version', '') is not null
     and nullif(new.raw_user_meta_data->>'legal_accepted_at', '') is not null then
    insert into public.legal_consents (
      user_id, policy_version, terms_accepted, privacy_accepted,
      acceptable_use_accepted, accepted_at, acceptance_method
    ) values (
      new.id, new.raw_user_meta_data->>'legal_version', true, true, true,
      (new.raw_user_meta_data->>'legal_accepted_at')::timestamptz,
      'signup_checkbox'
    ) on conflict (user_id, policy_version) do nothing;
  end if;

  insert into public.credit_accounts (user_id)
  values (new.id)
  on conflict (user_id) do nothing;

  insert into public.credit_ledger (
    user_id, delta, balance_after, event_type, feature_id, idempotency_key
  )
  values (new.id, 2000, 2000, 'welcome_grant', 'signup', 'welcome:' || new.id)
  on conflict (idempotency_key) do nothing;
  return new;
end;
$$;

drop trigger if exists on_auth_user_created_vurenn on auth.users;
create trigger on_auth_user_created_vurenn
  after insert on auth.users
  for each row execute procedure public.initialize_vurenn_user();

insert into public.profiles (user_id, display_name)
select id, coalesce(raw_user_meta_data->>'display_name', split_part(email, '@', 1))
from auth.users
on conflict (user_id) do nothing;

insert into public.credit_accounts (user_id)
select id from auth.users
on conflict (user_id) do nothing;

insert into public.credit_ledger (
  user_id, delta, balance_after, event_type, feature_id, idempotency_key
)
select id, 2000, 2000, 'welcome_grant', 'signup', 'welcome:' || id
from auth.users
on conflict (idempotency_key) do nothing;

create or replace function public.spend_vurenn_credits(
  p_user_id uuid,
  p_amount integer,
  p_feature_id text,
  p_idempotency_key text,
  p_metadata jsonb default '{}'::jsonb
)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare
  next_balance integer;
  existing_balance integer;
begin
  if p_amount <= 0 then
    raise exception 'Credit amount must be positive';
  end if;

  select balance_after into existing_balance
  from public.credit_ledger
  where idempotency_key = p_idempotency_key
    and user_id = p_user_id;
  if found then
    return existing_balance;
  end if;

  update public.credit_accounts
  set balance = balance - p_amount,
      lifetime_spent = lifetime_spent + p_amount,
      updated_at = now()
  where user_id = p_user_id and balance >= p_amount
  returning balance into next_balance;

  if next_balance is null then
    raise exception 'INSUFFICIENT_CREDITS';
  end if;

  insert into public.credit_ledger (
    user_id, delta, balance_after, event_type, feature_id,
    idempotency_key, metadata
  ) values (
    p_user_id, -p_amount, next_balance, 'usage', p_feature_id,
    p_idempotency_key, coalesce(p_metadata, '{}'::jsonb)
  );
  return next_balance;
end;
$$;

create or replace function public.grant_vurenn_credits(
  p_user_id uuid,
  p_amount integer,
  p_feature_id text,
  p_idempotency_key text,
  p_metadata jsonb default '{}'::jsonb
)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare
  next_balance integer;
  existing_balance integer;
begin
  if p_amount <= 0 then
    raise exception 'Credit amount must be positive';
  end if;

  select balance_after into existing_balance
  from public.credit_ledger
  where idempotency_key = p_idempotency_key
    and user_id = p_user_id;
  if found then
    return existing_balance;
  end if;

  insert into public.credit_accounts (user_id)
  values (p_user_id)
  on conflict (user_id) do nothing;

  update public.credit_accounts
  set balance = balance + p_amount,
      lifetime_granted = lifetime_granted + p_amount,
      updated_at = now()
  where user_id = p_user_id
  returning balance into next_balance;

  insert into public.credit_ledger (
    user_id, delta, balance_after, event_type, feature_id,
    idempotency_key, metadata
  ) values (
    p_user_id, p_amount, next_balance, 'purchase', p_feature_id,
    p_idempotency_key, coalesce(p_metadata, '{}'::jsonb)
  );
  return next_balance;
end;
$$;

create or replace function public.refund_vurenn_credits(
  p_user_id uuid,
  p_amount integer,
  p_feature_id text,
  p_idempotency_key text,
  p_metadata jsonb default '{}'::jsonb
)
returns integer
language plpgsql
security definer set search_path = public
as $$
declare
  next_balance integer;
  existing_balance integer;
begin
  if p_amount <= 0 then
    raise exception 'Credit amount must be positive';
  end if;

  select balance_after into existing_balance
  from public.credit_ledger
  where idempotency_key = p_idempotency_key
    and user_id = p_user_id;
  if found then
    return existing_balance;
  end if;

  update public.credit_accounts
  set balance = balance + p_amount,
      lifetime_spent = greatest(0, lifetime_spent - p_amount),
      updated_at = now()
  where user_id = p_user_id
  returning balance into next_balance;

  if next_balance is null then
    raise exception 'Credit account not found';
  end if;

  insert into public.credit_ledger (
    user_id, delta, balance_after, event_type, feature_id,
    idempotency_key, metadata
  ) values (
    p_user_id, p_amount, next_balance, 'refund', p_feature_id,
    p_idempotency_key, coalesce(p_metadata, '{}'::jsonb)
  );
  return next_balance;
end;
$$;

alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.subscriptions enable row level security;
alter table public.profiles enable row level security;
alter table public.legal_consents enable row level security;
alter table public.credit_accounts enable row level security;
alter table public.credit_ledger enable row level security;
alter table public.credit_purchases enable row level security;
alter table public.app_settings enable row level security;
alter table public.api_keys enable row level security;

drop policy if exists "Users manage their conversations" on public.conversations;
create policy "Users manage their conversations"
  on public.conversations for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users manage their messages" on public.messages;
create policy "Users manage their messages"
  on public.messages for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users read their subscription" on public.subscriptions;
create policy "Users read their subscription"
  on public.subscriptions for select
  using (auth.uid() = user_id);

drop policy if exists "Users manage their profile" on public.profiles;
create policy "Users manage their profile"
  on public.profiles for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "Users read their credit account" on public.credit_accounts;
create policy "Users read their credit account"
  on public.credit_accounts for select
  using (auth.uid() = user_id);

drop policy if exists "Users read their credit ledger" on public.credit_ledger;
create policy "Users read their credit ledger"
  on public.credit_ledger for select
  using (auth.uid() = user_id);

drop policy if exists "Users read their credit purchases" on public.credit_purchases;
create policy "Users read their credit purchases"
  on public.credit_purchases for select
  using (auth.uid() = user_id);

revoke all on public.conversations from anon;
revoke all on public.messages from anon;
revoke all on public.subscriptions from anon;
revoke all on public.profiles from anon;
revoke all on public.legal_consents from public, anon, authenticated;
revoke all on public.credit_accounts from anon;
revoke all on public.credit_ledger from anon;
revoke all on public.credit_purchases from anon;
revoke all on public.app_settings from anon, authenticated;
grant select, insert, update, delete on public.conversations to authenticated;
grant select, insert, update, delete on public.messages to authenticated;
grant select on public.subscriptions to authenticated;
grant select on public.profiles to authenticated;
revoke insert, update on public.profiles from authenticated;
grant insert (
  user_id, display_name, occupation, goals, response_style,
  response_preferences, onboarding_completed, onboarding_skipped,
  security_prompt_dismissed, camera_unlock_enabled
) on public.profiles to authenticated;
grant update (
  display_name, occupation, goals, response_style,
  response_preferences, onboarding_completed, onboarding_skipped,
  security_prompt_dismissed, camera_unlock_enabled, updated_at
) on public.profiles to authenticated;
grant select on public.credit_accounts to authenticated;
grant select on public.credit_ledger to authenticated;
grant select on public.credit_purchases to authenticated;

revoke all on function public.spend_vurenn_credits(
  uuid, integer, text, text, jsonb
) from public, anon, authenticated;
revoke all on function public.grant_vurenn_credits(
  uuid, integer, text, text, jsonb
) from public, anon, authenticated;
revoke all on function public.refund_vurenn_credits(
  uuid, integer, text, text, jsonb
) from public, anon, authenticated;
grant execute on function public.spend_vurenn_credits(
  uuid, integer, text, text, jsonb
) to service_role;
grant execute on function public.grant_vurenn_credits(
  uuid, integer, text, text, jsonb
) to service_role;
grant execute on function public.refund_vurenn_credits(
  uuid, integer, text, text, jsonb
) to service_role;
