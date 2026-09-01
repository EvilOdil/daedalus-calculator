-- Daedalus Calculator - Supabase schema.
-- Run once in the Supabase SQL editor (Dashboard -> SQL Editor -> New query).

create table if not exists public.profiles (
    kind        text        not null,
    id          text        not null,
    document    jsonb       not null,
    updated_at  timestamptz not null default now(),
    primary key (kind, id)
);

comment on table public.profiles is
    'One row per component profile or setup. `document` is the Pydantic model '
    'serialised as JSON; the app validates it on load, so a malformed row is '
    'reported and skipped rather than breaking the library.';

-- Kinds are fixed by the application; a typo here would silently create an
-- invisible profile, so reject it at the database instead.
alter table public.profiles drop constraint if exists profiles_kind_check;
alter table public.profiles add constraint profiles_kind_check
    check (kind in ('motors', 'props', 'escs', 'batteries', 'frames', 'payloads', 'setups'));

create index if not exists profiles_kind_idx on public.profiles (kind);

create or replace function public.touch_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
    before update on public.profiles
    for each row execute function public.touch_updated_at();

-- The app connects with the service_role key and is the only client, so RLS is
-- enabled with no permissive policy: anon and authenticated keys can read
-- nothing. Access control is the app's password gate, not Postgres.
alter table public.profiles enable row level security;
