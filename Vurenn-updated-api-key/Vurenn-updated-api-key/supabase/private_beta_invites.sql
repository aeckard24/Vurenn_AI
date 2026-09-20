-- Additive private-beta invitation migration for Vurenn.
-- Safe to run more than once. It does not delete or rewrite existing user data.

alter table public.profiles
  add column if not exists beta_access boolean not null default false;
alter table public.profiles
  add column if not exists beta_invited_at timestamptz;

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

-- Prevent signed-in users from granting themselves beta access directly.
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
