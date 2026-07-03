-- 001_listings.sql — Antiques listing pipeline state model
--
-- Paste into Supabase Studio → SQL Editor → Run.
-- Archie's project only. RLS is ON with NO anon policies — service-key access only.
-- Morley applies this by hand; code never runs DDL.

-- ---------------------------------------------------------------------------
-- tables
-- ---------------------------------------------------------------------------

create table if not exists public.listings (
    id            uuid primary key default gen_random_uuid(),
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    status        text not null default 'draft'
                  check (status in ('draft','priced','approved','listed','sold','shipped','rejected','error')),
    source        text,
    title         text,
    description   text,
    category_guess text,
    appraisal     jsonb,
    photos        jsonb,
    pricing       jsonb,
    approval      jsonb,
    provider      jsonb,
    shipping      jsonb,
    notes         text
);

-- updated_at trigger — keeps the row fresh on every patch.
create or replace function public.touch_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_listings_touch on public.listings;
create trigger trg_listings_touch
    before update on public.listings
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- row-level security
-- ---------------------------------------------------------------------------

alter table public.listings enable row level security;

-- NO anon policies. All access goes through the service key (server-side only).
-- The service role bypasses RLS, so these policies only matter if someone
-- accidentally queries with the anon key — in which case they get nothing.

-- Drop any stray policies from prior iterations (safe — if not exists).
drop policy if exists "no_anon_select" on public.listings;
drop policy if exists "no_anon_insert" on public.listings;
drop policy if exists "no_anon_update" on public.listings;
drop policy if exists "no_anon_delete" on public.listings;

-- ---------------------------------------------------------------------------
-- storage bucket
-- ---------------------------------------------------------------------------

-- Private bucket: signed URLs required to view photos.
insert into storage.buckets (id, name, public)
values ('listing-photos', 'listing-photos', false)
on conflict (id) do nothing;

-- Grant the service role full access to the bucket (it bypasses RLS anyway,
-- but being explicit doesn't hurt). Anon gets nothing.
-- These policies are on storage.objects, not public.listings.
drop policy if exists "service_all_listing_photos" on storage.objects;
create policy "service_all_listing_photos" on storage.objects
    for all
    using (bucket_id = 'listing-photos')
    with check (bucket_id = 'listing-photos');

-- The service role bypasses RLS, so the policy above is belt-and-suspenders.
-- Anon key queries against storage.objects for this bucket will be denied
-- because there is no anon-friendly policy.
