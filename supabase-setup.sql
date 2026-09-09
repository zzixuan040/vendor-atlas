-- Vendor Atlas — shared sourcing workflow state.
-- Paste this into your Supabase project's SQL editor and run it once.
--
-- Scope note: this stores ONLY human workflow state. Vendor facts stay in
-- vendors.json in git, refreshed weekly by scripts/scrape_vendors.py. The two
-- never share a store, which is what keeps an AI refresh from touching a
-- sourcing decision.

create table if not exists vendor_workflow (
  vendor_id   text primary key,          -- matches "id" in vendors.json
  status      text        not null default 'New',
  owner       text,                      -- null = Unassigned
  priority    text        not null default 'Medium',
  notes       text        not null default '',
  next_action text        not null default '',
  updated_at  timestamptz not null default now(),
  updated_by  text
);

create table if not exists vendor_activity (
  id         bigserial   primary key,
  vendor_id  text        not null,
  ts         timestamptz not null default now(),
  actor      text,
  action     text        not null,       -- e.g. 'status', 'owner', 'priority'
  old_value  text,
  new_value  text
);

create index if not exists vendor_activity_vendor_idx on vendor_activity (vendor_id, ts desc);

alter table vendor_workflow enable row level security;
alter table vendor_activity enable row level security;

-- Open read/write to the anon (public) key.
--
-- TRADEOFF, stated plainly: anyone who can load the dashboard can also change
-- workflow state, because there is no login. That is deliberate for an internal
-- team prototype — the brief explicitly excludes authentication and granular
-- permissions. Vendor facts are not writable from the browser at all (they live
-- in git), so the blast radius is workflow fields only, and every change is
-- recorded in vendor_activity. Add Supabase Auth + per-user policies before
-- putting anything sensitive in here.
drop policy if exists vendor_workflow_anon_all on vendor_workflow;
create policy vendor_workflow_anon_all on vendor_workflow
  for all to anon using (true) with check (true);

drop policy if exists vendor_activity_anon_all on vendor_activity;
create policy vendor_activity_anon_all on vendor_activity
  for all to anon using (true) with check (true);
